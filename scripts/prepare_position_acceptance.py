# SPDX-License-Identifier: Apache-2.0
"""Explicit synthetic NR-02 setup outside the checkout; never run as acceptance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from veqtor_docx import inspect_document
from veqtor_docx.synthetic import generate_demo_rounds
from capture_position_session import private_write
from check_position_acceptance import BASELINE_SCHEMA, REASONING_EFFORTS, baseline, client_selection, sha


def prepare(directory, installation_path, *, model, reasoning_effort):
    selection = client_selection(model, reasoning_effort)
    directory, installation_path = Path(directory).absolute(), Path(installation_path)
    installation = json.loads(installation_path.read_bytes())
    source_root = Path(installation["source_root"]).resolve()
    if directory.resolve().is_relative_to(source_root):
        raise ValueError("synthetic acceptance must stay outside checkout")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    private_write(directory / "installation.json", installation_path.read_bytes())
    original = directory / "synthetic-original"
    files = generate_demo_rounds(original, profile="paragraph-edits")
    folders = {"original": str(original), **{n: str(directory / ("synthetic-" + n))
               for n in ("moved", "conflict", "first", "other")}}
    for name in ("first", "other"):
        Path(folders[name]).mkdir(mode=0o700)
    for path in files:
        shutil.copy2(path, Path(folders["first"]) / path.name)
    first = files[0]
    ref = inspect_document(str(first), "browse")["paragraphs"][0]["paragraph_ref"]
    binding = {"path": first.name, "file_sha256": sha(first.read_bytes()), "reference": ref}
    ids = [f"pos_{i:032x}" for i in range(1, 6)]

    def content(title, outcome, **changes):
        return dict(title=title, desired_outcome=outcome, fallback=None, fallback_conditions=None,
            rationale=None, related_position_ids=[], content_origin="user_instruction",
            business_decision="not_required", sources=[]) | changes

    contents = [
        content("Payment", "Pay within 30 days after receipt.", sources=[binding]),
        content("Conditional concession", "Retain 30-day payment.", fallback="Allow 60 days.",
                fallback_conditions="Only against an unconditional bank guarantee.", rationale="Limit unsecured exposure."),
        content("Linked security", "Obtain the bank guarantee before extending credit.", related_position_ids=[ids[1]]),
        content("Business question", "Confirm the maximum unsecured credit amount.", business_decision="pending"),
        content("Model proposal", "Consider a quarterly reconciliation call.", content_origin="model_proposal"),
    ]
    expected = {"schema_version": BASELINE_SCHEMA, "producer": installation["producer"], "folders": folders,
        "client_selection": selection,
        "initial_positions": [dict(position_id=pid, version=1, content=c, confirmation=None, lifecycle="active")
                              for pid, c in zip(ids, contents)],
        "updated_content": content("Payment", "Pay within 45 days after receipt."),
        "confirmation_statement": "The user explicitly confirmed these displayed version 1 positions; business decision remains pending.",
        "conflict_contents": [content("Concurrent A", "Accept 60 days with security."),
                              content("Concurrent B", "Accept 75 days with security.")],
        "source_files": {path.name: sha(path.read_bytes()) for path in files}, "selected_current_file": files[1].name}
    baseline(expected)
    private_write(directory / "baseline.json", json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2).encode())
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--installation", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", required=True, choices=REASONING_EFFORTS)
    args = parser.parse_args()
    print(prepare(args.bundle, args.installation, model=args.model, reasoning_effort=args.reasoning_effort))


if __name__ == "__main__":
    main()
