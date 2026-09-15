# Original App Server source profile v1

`nr03-app-server-original.v1` is a bounded input contract for NR-03. It is
separate from legacy `codex exec --json`. Old raw/prefix pairs retain their
original verdict. In particular, the historical A brief remains REFUSE.
Neither the source identity relation nor a synthetic fixture proves native
acceptance, log authenticity, legal authority, journal completeness or Word QA.

## Qualified producer relationship

This profile is pinned to Codex `0.154.0-alpha.6.2`, embedded source commit
`b5bffd3ec4db487e7e3dec59663875b0ef7b72ca`, executable SHA-256
`ecad78dbf98adb89ec475edac86630406cbe59d9f3070b17d88065f136b94bcb`.
The launcher also checks the installed version and embedded commit. These are
static identification checks, not a reproducible build or signed log claim.

The pinned [router](https://github.com/openai/codex/blob/b5bffd3ec4db487e7e3dec59663875b0ef7b72ca/codex-rs/core/src/tools/router.rs),
[MCP handler](https://github.com/openai/codex/blob/b5bffd3ec4db487e7e3dec59663875b0ef7b72ca/codex-rs/core/src/tools/handlers/mcp.rs),
and [MCP lifecycle](https://github.com/openai/codex/blob/b5bffd3ec4db487e7e3dec59663875b0ef7b72ca/codex-rs/core/src/mcp_tool_call.rs)
preserve the evaluated invocation ID. The
[App Server item conversion and notification envelopes](https://github.com/openai/codex/blob/b5bffd3ec4db487e7e3dec59663875b0ef7b72ca/codex-rs/app-server-protocol/src/protocol/v2/item.rs)
preserve that ID and identify the thread and turn. This establishes the key
`K = (threadId, turnId, item.id)` within the bound connection/run. Legacy CLI
projected IDs, equality of arbitrary strings and synthetic flags establish no
such relation. This profile does not use trace events or a projector bridge.

## Original capture and isolation

The launcher uses the installed `app-server --stdio --strict-config` and
documented TOML `-c` overrides. Exec-only ignore flags are not supported here.
Only the child environment selects a fresh private `CODEX_HOME`. The parent
environment, persistent config and authentication are unchanged. A file-backend
auth snapshot lives only in the private temporary runtime, outside the checkout
and evidence; it is deleted with that runtime, including any token refresh. It
is never copied into evidence or placed in commands/receipts. Missing file auth
refuses; no alternate keyring/login flow is inferred.

Original initialization, outgoing requests, all received stdio bytes, errors,
diagnostics and an independently hashed transport ordering journal are retained.
Initialization suppresses no notifications. There is no MCP-only filtered log.
Startup bytes remain private until the configuration check passes. Nonempty
inherited layers, including disabled ones, refuse before a model turn or
publication of those bytes; they may contain credentials. After the check the
entire original startup prefix is moved without filtering into the ongoing
capture. The effective configuration and layers must match the single selected MCP
server and frozen model/effort before a model turn starts; active inherited
configuration, plugins/hooks/instructions or loaded instruction sources refuse.
Unknown notifications and non-MCP actions refuse. Failures after the startup
configuration gate retain the public capture but cannot produce passing evidence.
A refusal at the private gate has no passing source receipt or model turn. This deliberately narrow
profile may refuse new notifications from even the pinned build until separately
reviewed; it never skips them silently.

A resumed turn gets an exact private copy of its own earlier complete prefix;
the earlier evidence is not opened for append. The original resulting session
prefix is copied through the actual matching `task_complete` before cleanup.
Missing persisted completion refuses. `thread/read`, final snapshots and
backfill cannot supply missing original starts or manufacture model outputs.

The v2 stage receipt contains a versioned source binding for build/executable,
capture/parser/policy hashes, original raw/request/ordering/prefix/stderr hashes,
connection/run/thread/turn, effective launch selection and the entire outer
receipt context. The outer context continues to bind frozen installation,
business stimulus, workflow, baseline, selected files, ancestry and before/after
state. Complete actual turn context in the prefix must agree with model/effort.
The existing delivery binding additionally binds that prefix to the receipt.

## Inventory and result conversion

There must be exactly one original start and terminal per K, with full typed
operation/arguments and exact millisecond lifecycle correspondence to the
persisted core completion. All calls, including failures and optional tools,
must reconcile. No later value can replace a missing earlier occurrence.

The pinned [result conversion](https://github.com/openai/codex/blob/b5bffd3ec4db487e7e3dec59663875b0ef7b72ca/codex-rs/app-server-protocol/src/protocol/v2/mcp.rs)
copies `content`, `structuredContent` and `_meta`, but omits core `isError`.
The checker retains the complete original core item/result and separately
verifies status/error semantics. Missing optional core values map to the explicit
null App Server fields; absent core `isError` is allowed, present values must be
booleans. Unknown fields, nonfinite numbers and duplicate JSON keys refuse.
Core durations convert from seconds/nanoseconds to integer milliseconds; full
arguments, arrays, metadata, producer and public payloads are not normalized.
An in-memory adapter feeds frozen document/journal consumers; it never writes a
replacement CLI transcript. Failed envelopes cannot become successful payloads.

## Model delivery grammar and occurrence ownership

Terminals are a full producer payload, a complete text-block array, or a
consistent complete MCP result envelope. A finite item may contain at most one
exact `{file,result}` or `{file,index,result}` label and at most one exact
`{status:"fulfilled",value}` wrapper, in either order. Complete separate blocks
and one flat collection are accepted. Arbitrary recursion, repeated wrappers,
rejected settlements, incomplete JSON and malformed collection members refuse.

Direct original actions/outputs must have `call_id = K.item.id`, with exact
operation/typed arguments. Exec attribution uses the original persisted inner
`McpToolCall.id = K.item.id` inside exactly one open current-turn exec before its
output. JavaScript is never interpreted and parent aliases are never inferred.
The complete eligible set is matched before any label is tested. Labels cannot
break a tie; neither iteration order nor consuming a candidate can do so.
Different K values with equal sequential results are separate occurrences;
indistinguishable equal results in one batch remain ambiguous. Duplicate output
observations and replay cannot create later credit.

`file` must be an exact selected absolute operand or a basename unique across
all current-turn document operands, with exactly one operand role. Index is a
nonnegative JSON integer matching the exact paragraph reference in the relevant
inspect selection or verify anchor. Unsupported index/ref combinations refuse.
Labels constrain the already unique source occurrence, not its identity.

Synthetic source protocol fixtures explicitly declare their provenance and
preserve original legacy fixture bytes and action/output facts for comparison.
Their additional lifecycle/context records describe the same invocation
definitions. They go through the production parser and consumers without a
synthetic bypass. They do not upgrade the preserved pair to native evidence.
Raw complete journal pages plus a clipped model page still fail presentation,
even if a later terminal page is visible. Native campaigns, human review,
rendered Word inspection, exact-SHA review, merge and release remain separate.
