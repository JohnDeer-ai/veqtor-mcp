<!-- SPDX-License-Identifier: Apache-2.0 -->

# Contributing

Contributions are accepted under Apache-2.0, consistent with Section 5 of the
license.

This project uses the Developer Certificate of Origin. Add a `Signed-off-by`
line to every commit:

```text
Signed-off-by: Your Name <you@example.com>
```

Use `git commit -s` to add it automatically.

Do not submit real client documents, confidential matter data, or generated
fixtures derived from real client documents.

Use GitHub Issues for reproducible bugs and feature requests. Reports must use
synthetic documents and must not contain local paths, `.veqtor` journals,
credentials or contract wording from a real matter. Suspected vulnerabilities
must follow [SECURITY.md](SECURITY.md), not a public issue.

Before submitting a development change, run:

### Development checks

```bash
uv lock --check
uv sync --frozen --all-extras
uv run --frozen pytest -q
uvx ruff==0.15.21 check .
LOCKED_REQUIREMENTS="$(mktemp)"
uv export --frozen --no-dev --no-emit-project \
  --format requirements-txt --output-file "$LOCKED_REQUIREMENTS"
uvx pip-audit==2.10.1 --requirement "$LOCKED_REQUIREMENTS" \
  --require-hashes --disable-pip --progress-spinner off
uv build --clear
uvx twine check dist/*.whl dist/*.tar.gz
```

### Frozen-release checks

After the development checks, run the commands below only from a frozen
release candidate where `[project].version` exactly matches
`scripts/release_contract.py::VERSION`. They intentionally reject a
development-only tree such as `0.4.0.dev0` whose identity differs from the
frozen release contract. See [RELEASING.md](RELEASING.md) for the complete
release process.

```bash
uv run --frozen python scripts/build_mcpb.py \
  --source-root . --out-dir dist --stage-dir /tmp/veqtor-mcpb-stage
uv run --frozen python scripts/check_release_artifacts.py \
  --source-root . --commit HEAD dist/*.whl dist/*.tar.gz
uv run --frozen python scripts/check_mcpb_artifact.py \
  --source-root . --commit HEAD dist/*.mcpb
npx --yes @anthropic-ai/mcpb@2.1.2 validate \
  /tmp/veqtor-mcpb-stage/manifest.json
```

Changes to the permanent `decision_record.v1` tool pairs or compact projection
must preserve the historical golden fixtures. Add compatible fields or a new
append-only tool pair; do not rewrite old records merely to match the current
producer version.
