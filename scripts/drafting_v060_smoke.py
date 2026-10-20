# SPDX-License-Identifier: Apache-2.0
"""Run the paragraph/thread workflow through a real stdio MCP server.

Uses only generated fixtures. --output-dir must be new or empty; it retains
working.docx, exact source backups, and report.json for separate native review.
--python selects the server interpreter, including an isolated installed wheel.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import jsonschema
from lxml import etree
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client

from veqtor_docx import generate_demo_rounds

from drafting_smoke import (
    _current_text, _document, _parts, _payload, _require,
    _revision_signatures, _sha, _w,
)

DEFAULT = {"name": "Shilov, Ilya", "initials": "IS"}
SELECTED = {"name": "Selected Smoke Reviewer", "initials": "SR"}
COUNTERPARTY = {"name": "Synthetic Counterparty Comment Author", "initials": "SC"}
ORIGINAL_TITLE = "Master Development and Supply Agreement"
EDITED_TITLE = "Master Development and Services Agreement"
FINAL_TITLE = "Replacement agreement title."


def _root_comment_signature(path):
    root = etree.fromstring(_parts(path)["word/comments.xml"])[0]
    for paragraph in root.iter(_w("p")):
        paragraph.attrib.pop("{http://schemas.microsoft.com/office/word/2010/wordml}paraId", None)
        paragraph.attrib.pop("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable", None)
    return etree.tostring(root, method="c14n", exclusive=True)


async def smoke(output_dir: Path, interpreter: str) -> dict:
    output_dir = output_dir.expanduser().absolute()
    _require(not output_dir.is_symlink(), "output directory must not be a symlink")
    if output_dir.exists():
        _require(output_dir.is_dir() and not any(output_dir.iterdir()),
                 "--output-dir must be a new or empty directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve()
    fixtures = output_dir / "fixtures"
    generate_demo_rounds(fixtures)
    working = output_dir / "working.docx"
    working.write_bytes((fixtures / "round-2-counterparty-redline.docx").read_bytes())
    baseline = _revision_signatures(_document(working))
    _require(bool(baseline), "fixture must contain counterparty revisions")
    environment = {**os.environ, "VEQTOR_TRACKED_CHANGE_AUTHOR": DEFAULT["name"],
                   "VEQTOR_COMMENT_INITIALS": DEFAULT["initials"]}
    environment.pop("PYTHONPATH", None)
    parameters = StdioServerParameters(command=interpreter,
        args=["-m", "veqtor_mcp.server"], env=environment, cwd=str(output_dir))
    producer, steps, backups = None, [], set()

    async with Client(stdio_client(parameters), mode="auto") as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

        async def call(name, arguments):
            nonlocal producer
            tool = tools[name]
            jsonschema.Draft202012Validator(tool.input_schema).validate(arguments)
            result = _payload(await client.call_tool(name, arguments))
            jsonschema.Draft202012Validator(tool.output_schema).validate(result)
            observed = result.get("producer")
            _require(isinstance(observed, dict) and observed.get("build"),
                     f"{name} has no runtime provenance")
            if producer is None:
                producer = observed
            _require(observed == producer, "runtime provenance changed during workflow")
            return result

        async def save(label, build, profile=None):
            located = await call("inspect_document",
                {"path": str(working), "mode": "browse", "max_items": 100})
            refs = {item["text"]: item["paragraph_ref"] for item in located["paragraphs"]}
            before = working.read_bytes()
            _require(located["file_sha256"] == _sha(before), "inspection snapshot differs")
            args = {"source_path": str(working), "file_sha256": _sha(before),
                    "operations": build(refs)}
            if profile is not None:
                args["author_profile"] = profile
            previous_backups = set((output_dir / ".veqtor-backups").glob("*.docx"))
            preview = await call("preflight_document_edits", args)
            _require(preview["batch_applicable"], f"{label}: preflight refused {preview}")
            _require(working.read_bytes() == before, "preflight changed working file")
            _require(set((output_dir / ".veqtor-backups").glob("*.docx")) == previous_backups,
                     "preflight created a backup")
            expected = profile or DEFAULT
            _require((preview["tracked_change_author"], preview["comment_initials"]) ==
                     (expected["name"], expected["initials"]), "author selection differs")
            result = await call("apply_document_edits",
                               {**args, "preflight_proof": preview["preflight_proof"]})
            _require(result["status"] == "ok" and result["save_mode"] == "in_place"
                     and result["output_path"] == str(working), "file was not saved in place")
            _require(result["preflight_binding_status"] == "verified", "unbound save")
            backup = Path(result["backup_path"])
            _require(backup.parent == output_dir / ".veqtor-backups"
                     and str(backup) not in backups and backup.read_bytes() == before,
                     "backup is not a new exact source copy")
            _require(result["source_sha256"] == result["backup_sha256"] == _sha(before)
                     and result["output_sha256"] == _sha(working.read_bytes()), "save hashes differ")
            backups.add(str(backup))
            _require(not (baseline - _revision_signatures(_document(working))),
                     "an unrelated counterparty revision changed")
            steps.append({"label": label, "author_profile": expected,
                          "source_sha256": result["source_sha256"],
                          "output_sha256": result["output_sha256"],
                          "backup_path": str(backup), "backup_sha256": result["backup_sha256"]})
            return result

        async def comments():
            result = await call("read_document_comments", {"path": str(working)})
            _require(result["file_sha256"] == _sha(working.read_bytes())
                     and result["next_offset"] is None, "comment snapshot differs")
            return result["comments"]

        await save("configured_author_insertion", lambda refs: [{
            "kind": "insert_paragraph", "paragraph_ref": refs[ORIGINAL_TITLE],
            "position": "before", "text": "Default-author preface."}])
        await save("selected_author_source_stable_batch", lambda refs: [
            {"kind": "insert_paragraph", "paragraph_ref": refs[ORIGINAL_TITLE],
             "position": position, "text": text}
            for position, text in [
                ("after", "First inserted clause."), ("after", "Second inserted clause."),
                ("before", "Selected-author preface.")]
        ] + [{"kind": "replace_text", "paragraph_ref": refs[ORIGINAL_TITLE],
              "old_text": "Supply", "new_text": "Services"}], SELECTED)
        _require([_current_text(p) for p in _document(working).find(_w("body"))[:5]] == [
            "Default-author preface.", "Selected-author preface.", EDITED_TITLE,
            "First inserted clause.", "Second inserted clause."], "source references shifted")
        await save("whole_mixed_paragraph", lambda refs: [{
            "kind": "replace_paragraph", "paragraph_ref": refs[EDITED_TITLE],
            "old_text": EDITED_TITLE, "new_text": FINAL_TITLE}], SELECTED)
        await save("synthetic_foreign_root_comment", lambda refs: [{
            "kind": "add_comment", "paragraph_ref": refs["Definitions"],
            "quote": "Definitions", "text": "Please confirm the definitions."}], COUNTERPARTY)
        root = (await comments())[-1]
        # A legacy root receives only native paragraph/thread identifiers when
        # its first reply promotes it to a thread; original content stays exact.
        root_xml = _root_comment_signature(working)
        await save("selected_author_reply", lambda _: [{
            "kind": "reply_comment", "comment_id": root["id"], "text": "Scope agreed."}], SELECTED)
        reply = (await comments())[-1]
        _require(reply["parent_comment_id"] == root["id"] and reply["thread_root_id"] == root["id"]
                 and reply["author"] == SELECTED["name"] and reply["initials"] == SELECTED["initials"],
                 "reply identity or thread linkage differs")
        await save("resolve_thread_via_reply", lambda _: [{
            "kind": "set_comment_resolved", "comment_id": reply["id"], "resolved": True}], SELECTED)
        _require(all(item["resolved"] is True for item in await comments()), "thread did not resolve")
        await save("reopen_and_default_author_reply", lambda _: [
            {"kind": "set_comment_resolved", "comment_id": root["id"], "resolved": False},
            {"kind": "reply_comment", "comment_id": root["id"], "text": "Confirmed after reopening."}])
        rows = await comments()
        _require(all(item["resolved"] is False for item in rows), "thread did not reopen")
        _require(rows[-1]["author"] == DEFAULT["name"] and rows[-1]["initials"] == DEFAULT["initials"],
                 "default reply author differs")
        _require(_root_comment_signature(working) == root_xml, "foreign root comment changed")
        document = _document(working)
        authors = {node.get(_w("author")) for node in document.iter(_w("ins"))}
        _require({DEFAULT["name"], SELECTED["name"]} <= authors, "chosen revision authors missing")
        report = {"status": "passed", "producer": producer, "protocol_version": client.protocol_version,
                  "working_path": str(working), "server_interpreter": interpreter,
                  "schema_validation": "all MCP inputs and outputs", "save_count": len(steps),
                  "exact_backup_count": len(backups), "existing_revisions_preserved": True,
                  "existing_revision_count": sum(baseline.values()), "foreign_root_preserved": True,
                  "comment_count": len(rows), "configured_profile": DEFAULT, "selected_profile": SELECTED,
                  "final_sha256": _sha(working.read_bytes()), "native_word_verified": False, "steps": steps}
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable, dest="interpreter",
                        help="interpreter containing the server package")
    args = parser.parse_args()
    report = asyncio.run(smoke(args.output_dir, args.interpreter))
    print(json.dumps({key: report[key] for key in
        ("status", "producer", "working_path", "save_count", "exact_backup_count")}, indent=2))


if __name__ == "__main__":
    main()
