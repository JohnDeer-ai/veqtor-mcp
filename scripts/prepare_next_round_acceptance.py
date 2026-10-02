# SPDX-License-Identifier: Apache-2.0
"""Freeze invented NR-03 inputs outside checkout before native workflow capture."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import zipfile

from check_codex_acceptance import _digest, _file_sha256, _json, _read, _require
from check_position_install import verify as verify_install
from capture_position_session import private_write
from nr03_scenario import (
    AUTHOR, C2, C3, EFFORTS, MODEL, ORACLE_FILES, VARIANTS, VERSION, WORKFLOW_FILES,
    document, oracle, package, seed_store, source_manifest, xml,
)
from veqtor_docx._ooxml import canonical_body_flow_v1, parse_xml, w

SCHEMA = "veqtor_next_round_baseline.v1"


def read_json(path):
    return _json(_read(Path(path)).decode())


def write_json(path, value):
    private_write(Path(path), json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode())


def installed(report):
    observed = verify_install(report["source_root"], report["commit"], report["tree"],
                              report["wheel"], report["sdist"], report["python"])
    _require(observed == report, "installed candidate no longer matches installation receipt")
    return observed


def inventory(folder):
    paths = sorted(Path(folder).rglob("*"))
    _require(not any(p.is_symlink() for p in paths), "symlink in synthetic matter")
    return {str(p): _file_sha256(str(p)) for p in paths if p.suffix.lower() == ".docx"}


def state(folder):
    path = Path(folder) / ".veqtor" / "deal-positions.json"
    return dict(docx=inventory(folder), store_sha256=_file_sha256(str(path)) if path.exists() else None)


def prepare(directory, installation_path, *, model, reasoning_effort, variant="main"):
    _require(model == MODEL and reasoning_effort in EFFORTS, "NR-03 requires explicit gpt-6-astra high/xhigh")
    _require(variant in VARIANTS, "unknown synthetic variant")
    report = installed(read_json(installation_path))
    root = Path(report["source_root"]).resolve()
    directory = Path(directory).absolute()
    _require(not directory.resolve().is_relative_to(root), "raw acceptance must stay outside checkout")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    matter = directory / "matter"
    matter.mkdir(mode=0o700)
    private_write(directory / "installation.json", _read(Path(installation_path)))
    # Capture only delivered workflow files for the native input. Oracle/replies
    # remain in the observer bundle; no selected expected tool calls are exposed.
    for name in WORKFLOW_FILES:
        destination = directory / "workflow" / name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_write(destination, (root / name).read_bytes())
    previous, source = matter / "previous-sent.docx", matter / "incoming-a.docx"
    private_write(previous, document("previous"))
    private_write(source, document("incoming-a", variant=variant))
    store = seed_store(matter, variant=variant)
    if variant == "existing-output":
        private_write(matter / "counter-a.docx", document("previous"))
    for round_name in ("a", "b"):
        (directory / f"client-{round_name}").mkdir(mode=0o700)
    # Freeze the human stimulus source as well as its exact bytes in each receipt.
    private_write(directory / "user-replies.md", (root / "docs/NR03_USER_REPLIES.md").read_bytes())
    frozen = dict(schema_version=SCHEMA, workflow_version=VERSION,
        prepared_ns=time.time_ns(), variant=variant, matter=str(matter),
        client_selection=dict(model=model, reasoning_effort=reasoning_effort),
        installation_sha256=_file_sha256(str(directory / "installation.json")),
        workflow_sha256=source_manifest(root, WORKFLOW_FILES),
        oracle_source_sha256=source_manifest(root, ORACLE_FILES), oracle=oracle(),
        initial_state=state(matter), store=store, author=AUTHOR,
        inputs={"a": dict(source=str(source), previous=None if variant == "no-previous" else str(previous),
                          output=str(matter / "counter-a.docx")),
                "b": dict(source=str(matter / "incoming-b.docx"), previous=str(matter / "counter-a.docx"),
                          output=str(matter / "counter-b.docx"))})
    write_json(directory / "baseline.json", frozen)
    return frozen


def prepare_second(directory):
    """Explicitly synthetic new incoming, preserving the verified real first output.

    Semantic texts/operations were frozen at prepare; opaque revision IDs come
    from the verified real first output. This never modifies that output.
    """
    from check_next_round_acceptance import check_round
    directory = Path(directory).absolute()
    baseline = read_json(directory / "baseline.json")
    _require(baseline["variant"] == "main", "sequential gate is the main scenario")
    check_round(directory, "a")
    source = Path(baseline["inputs"]["a"]["output"])
    output = Path(baseline["inputs"]["b"]["source"])
    with zipfile.ZipFile(source) as archive:
        parts = {n: archive.read(n) for n in archive.namelist()}
    root = parse_xml(parts["word/document.xml"])
    p = canonical_body_flow_v1(root.find(w("body"))).paragraphs[2].element
    _require(len(p) == 1 and p[0].tag == w("r") and len(p[0]) == 1
             and p[0][0].tag == w("t") and p[0][0].text == C3,
             "second input seed is not expected clean confidentiality")
    p[0][0].text = C2
    parts["word/document.xml"] = xml(root)
    private_write(output, package(parts))
    write_json(directory / "round-b-baseline.json", dict(
        schema_version="veqtor_next_round_second.v1", prepared_ns=time.time_ns(),
        baseline_sha256=_file_sha256(str(directory / "baseline.json")),
        first_output_sha256=_file_sha256(str(source)), source_sha256=_file_sha256(str(output)),
        state=state(baseline["matter"]), semantic_oracle_sha256=_digest(baseline["oracle"])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--installation")
    parser.add_argument("--model", choices=(MODEL,))
    parser.add_argument("--reasoning-effort", choices=EFFORTS)
    parser.add_argument("--variant", choices=VARIANTS, default="main")
    parser.add_argument("--second", action="store_true")
    args = parser.parse_args()
    if args.second:
        prepare_second(args.bundle)
    else:
        if not all((args.installation, args.model, args.reasoning_effort)):
            parser.error("initial preparation requires --installation --model --reasoning-effort")
        prepare(args.bundle, args.installation, model=args.model,
                reasoning_effort=args.reasoning_effort, variant=args.variant)
    print("Synthetic preparation frozen; native acceptance not yet established.")


if __name__ == "__main__":
    main()
