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

Before submitting a change, run:

```bash
uv sync --all-extras
uv lock --check
uv run pytest -q
uv build --clear
uvx twine check dist/*
uv run python scripts/check_release_artifacts.py dist/*
```

Changes to the permanent `decision_record.v1` tool pairs or compact projection
must preserve the historical golden fixtures. Add compatible fields or a new
append-only tool pair; do not rewrite old records merely to match the current
producer version.
