# SPDX-License-Identifier: Apache-2.0
"""Synthetic fixtures and process-local faults for the TypeScript MCP check.

This launcher is test-only. Production entry points never import it. Faults
are installed in a separate server process for each scenario, after the
synthetic fixture has been created. CLI arguments select only a synthetic
fixture directory or a fault mode, never an existing document.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from veqtor_docx import generate_demo_rounds


def generate(directory: Path) -> None:
    if any(directory.iterdir()):
        raise ValueError("fixture directory must be empty")
    generate_demo_rounds(directory / "generated")
    payload = (directory / "generated" / "round-1-outgoing-draft.docx").read_bytes()
    paths = {}
    for name in ("success", "word_lock", "before_publish", "after_publish", "invalid", "journal_finalize"):
        path = directory / f"{name}.docx"
        with path.open("xb") as target:
            target.write(payload)
        paths[name] = str(path)
    print(json.dumps(paths))


def serve(fault: str) -> None:
    from veqtor_docx import _drafting_io as io
    from veqtor_mcp import server
    from veqtor_docx import operation_journal

    real_replace = io.os.replace
    real_fsync = io.os.fsync
    real_journal_write = operation_journal._write
    published = False
    failed = False

    def injected_replace(*args, **kwargs):
        nonlocal published
        # The target is a fixture owned by this scenario. Lock bookkeeping and
        # other incidental replacements must not count as document publication.
        if args[1] != f"{fault}.docx":
            return real_replace(*args, **kwargs)
        if fault == "before_publish":
            raise OSError("PRIVATE_TYPESCRIPT_PUBLICATION_FAILURE")
        result = real_replace(*args, **kwargs)
        published = True
        return result

    def injected_fsync(*args, **kwargs):
        nonlocal failed
        if fault == "after_publish" and published and not failed:
            failed = True
            raise OSError("PRIVATE_TYPESCRIPT_DURABILITY_FAILURE")
        return real_fsync(*args, **kwargs)

    def injected_journal_write(fd, name, payload):
        nonlocal failed
        if fault == "journal_finalize" and name == "result.json" and not failed:
            failed = True
            raise OSError("PRIVATE_TYPESCRIPT_JOURNAL_FAILURE")
        return real_journal_write(fd, name, payload)

    operation_journal._write = injected_journal_write
    io.os.replace = injected_replace
    io.os.fsync = injected_fsync
    sys.argv = [sys.argv[0]]
    server.main()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fixtures = commands.add_parser("generate")
    fixtures.add_argument("directory", type=Path)
    faults = commands.add_parser("serve")
    faults.add_argument("fault", choices=("before_publish", "after_publish", "journal_finalize"))
    args = parser.parse_args()
    if args.command == "generate":
        generate(args.directory)
    else:
        serve(args.fault)


if __name__ == "__main__":
    main()
