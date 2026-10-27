# SPDX-License-Identifier: Apache-2.0
"""Exercise author drafting through a real stdio MCP server on synthetic files.

Run this with the interpreter containing the installed wheel, for example::

    /path/to/venv/bin/python scripts/drafting_smoke.py --output-dir /new/acceptance

The server command defaults to that interpreter plus ``-m veqtor_mcp.server``.
The configured author/initials are inherited from the environment; absent an
author setting, this user-workflow smoke uses ``Shilov, Ilya``. No real contract
is opened. The output directory must be new or empty. Keep it for Word/render
acceptance: it contains the final working.docx, exact backups and report.json.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

from lxml import etree
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client

from veqtor_docx import generate_demo_rounds


EXPECTED_TOOLS = frozenset({
    "list_rounds", "extract_redlines", "inspect_document", "map_rounds",
    "trace_paragraph_history", "preflight_edits", "apply_edits", "verify_quote",
    "export_decision_record", "read_deal_positions", "mutate_deal_positions",
    "read_deal_state", "mutate_deal_state", "read_round_issue_brief",
    "compare_incoming_version",
    "read_document_tables",
    "check_bilingual_conditions",
    "read_document_comments", "preflight_document_edits", "apply_document_edits",
    "read_document_revisions", "recover_document_operation",
})
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
TITLE_FIRST = "Master Development and Supply Agreement"
TITLE_FINAL = "Master Development and Production Agreement"
COMMENT_INITIAL = "Please confirm the manufacturing scope."
COMMENT_FINAL = "Scope confirmed; use the production title."


def _w(name: str) -> str:
    return f"{{{W_NS}}}{name}"


def _require(condition: bool, detail: str) -> None:
    # Acceptance checks must also execute when the interpreter uses -O.
    if not condition:
        raise RuntimeError(detail)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _document(path: Path) -> etree._Element:
    return etree.fromstring(_parts(path)["word/document.xml"])


def _revision_signatures(document: etree._Element) -> Counter:
    # Every original authored revision, including property/move revisions, must
    # survive. The baseline title has none, so none may legitimately disappear.
    return Counter(etree.tostring(node, method="c14n", exclusive=True)
                   for node in document.iter() if node.get(_w("author")) is not None)


def _title(document: etree._Element) -> etree._Element:
    paragraph = document.find(f"{_w('body')}/{_w('p')}")
    _require(paragraph is not None, "synthetic fixture has no title paragraph")
    return paragraph


def _current_text(element: etree._Element) -> str:
    return "".join(node.text or "" for node in element.iter(_w("t"))
                   if not any(parent.tag in {_w("del"), _w("moveFrom")}
                              for parent in node.iterancestors()))


def _own_title_insertion(document: etree._Element, author: str) -> etree._Element:
    insertions = [node for node in _title(document).iter(_w("ins"))
                  if node.get(_w("author")) == author]
    _require(len(insertions) == 1, "title must contain exactly one own insertion")
    return insertions[0]


def _comment_anchors(element: etree._Element, ident: str) -> tuple:
    return tuple((node.tag, tuple(sorted(node.attrib.items())))
                 for node in element.iter()
                 if node.tag in {_w("commentRangeStart"), _w("commentRangeEnd"), _w("commentReference")}
                 and node.get(_w("id")) == ident)


def _payload(result) -> dict:
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool failed: {result.content!r}")
    data = result.structured_content
    if not isinstance(data, dict):
        text = next((block.text for block in result.content if getattr(block, "type", None) == "text"), None)
        _require(text is not None, "MCP tool returned no structured or JSON text payload")
        data = json.loads(text)
    payload = data.get("result", data)
    _require(isinstance(payload, dict), "MCP payload is not an object")
    return payload


async def smoke(output_dir: Path, *, interpreter: str, author: str | None,
                initials: str | None) -> dict:
    output_dir = output_dir.expanduser().absolute()
    if output_dir.exists():
        _require(output_dir.is_dir() and not output_dir.is_symlink()
                 and not any(output_dir.iterdir()), "--output-dir must be a new or empty directory")
    else:
        output_dir.mkdir(parents=True)
    output_dir = output_dir.resolve()
    fixture_dir = output_dir / "fixtures"
    generate_demo_rounds(fixture_dir)
    original = (fixture_dir / "round-2-counterparty-redline.docx").read_bytes()
    working = output_dir / "working.docx"
    with working.open("xb") as target:
        target.write(original)
    baseline = _revision_signatures(_document(working))
    _require(bool(baseline), "round-2 fixture must contain existing counterparty revisions")
    _require(_current_text(_title(_document(working))) == TITLE_FIRST,
             "synthetic title differs from the acceptance scenario")

    environment = dict(os.environ)
    configured_author = author or environment.get("VEQTOR_TRACKED_CHANGE_AUTHOR", "Shilov, Ilya")
    environment["VEQTOR_TRACKED_CHANGE_AUTHOR"] = configured_author
    if initials is not None:
        environment["VEQTOR_COMMENT_INITIALS"] = initials
    parameters = StdioServerParameters(command=interpreter,
                                       args=["-m", "veqtor_mcp.server"], env=environment)
    producer = None
    profile = None
    insertion_attributes = None
    steps = []
    backups: set[str] = set()

    async with Client(stdio_client(parameters), mode="auto") as client:
        inventory = await client.list_tools()
        names = {tool.name for tool in inventory.tools}
        _require(names == EXPECTED_TOOLS,
                 f"expected the exact {len(EXPECTED_TOOLS)} development tools; observed {sorted(names)}")

        async def call(name: str, arguments: dict) -> dict:
            nonlocal producer
            result = _payload(await client.call_tool(name, arguments))
            observed = result.get("producer")
            _require(isinstance(observed, dict) and bool(observed.get("build")),
                     f"{name} did not identify its runtime build")
            if producer is None:
                producer = observed
            _require(observed == producer, "runtime version/build changed during the smoke")
            return result

        async def inspect() -> dict:
            result = await call("inspect_document", {
                "path": str(working), "mode": "browse", "max_items": 1,
            })
            _require(result["file_sha256"] == _sha(working.read_bytes()), "inspection snapshot differs")
            _require(result["paragraphs"][0]["paragraph_ref"]["paragraph_index"] == 0,
                     "inspection did not return the title paragraph")
            return result

        async def save(label: str, make_operations) -> dict:
            nonlocal profile, insertion_attributes
            located = await inspect()
            before = working.read_bytes()
            operations = make_operations(located["paragraphs"][0]["paragraph_ref"])
            arguments = {"source_path": str(working), "file_sha256": located["file_sha256"],
                         "operations": operations}
            before_backups = set((output_dir / ".veqtor-backups").glob("*.docx"))
            preview = await call("preflight_document_edits", arguments)
            _require(preview["batch_applicable"], f"{label} preflight refused: {preview}")
            _require(working.read_bytes() == before, "preflight changed the working document")
            _require(set((output_dir / ".veqtor-backups").glob("*.docx")) == before_backups,
                     "preflight created a backup")
            observed_profile = {"author": preview["tracked_change_author"],
                                "initials": preview["comment_initials"]}
            if profile is None:
                profile = observed_profile
            _require(profile == observed_profile, "configured author profile changed during the smoke")
            _require(profile["author"] == configured_author.strip(), "server did not use the configured author")
            if initials is not None:
                _require(profile["initials"] == initials, "server did not use the configured initials")
            result = await call("apply_document_edits", {
                **arguments, "preflight_proof": preview["preflight_proof"],
            })
            _require(result["status"] == "ok" and result["save_mode"] == "in_place", "save did not use in-place mode")
            _require(result["output_path"] == str(working), "working path changed")
            _require(result["preflight_binding_status"] == "verified", "preflight proof was not verified")
            _require(result["source_sha256"] == _sha(before), "save source hash differs")
            _require(result["output_sha256"] == _sha(working.read_bytes()), "save output hash differs")
            backup = Path(result["backup_path"])
            _require(backup.parent == output_dir / ".veqtor-backups", "backup is not a sibling backup")
            _require(str(backup) not in backups, "save reused a prior backup")
            _require(backup.read_bytes() == before and result["backup_sha256"] == _sha(before),
                     "backup does not preserve the exact prior source bytes")
            backups.add(str(backup))
            document = _document(working)
            _require(not (baseline - _revision_signatures(document)), "an existing counterparty revision changed")
            insertion = _own_title_insertion(document, profile["author"])
            if insertion_attributes is None:
                insertion_attributes = dict(insertion.attrib)
            _require(dict(insertion.attrib) == insertion_attributes,
                     "own insertion identity/author changed during iterative drafting")
            steps.append({"label": label, "source_sha256": result["source_sha256"],
                          "output_sha256": result["output_sha256"],
                          "backup_path": str(backup), "backup_sha256": result["backup_sha256"]})
            return result

        def replacement(old: str, new: str):
            return lambda ref: [{"kind": "replace_text", "paragraph_ref": ref,
                                 "old_text": old, "new_text": new}]

        async def comments() -> list[dict]:
            result = await call("read_document_comments", {"path": str(working)})
            _require(result["file_sha256"] == _sha(working.read_bytes()), "comments snapshot differs")
            _require(result["next_offset"] is None, "unexpected comment pagination in synthetic fixture")
            return result["comments"]

        await save("plain_text_to_own_revision", replacement("Supply", "Services"))
        await save("reedit_own_revision", replacement("Services", "Manufacturing"))
        await save("comment_on_own_insertion", lambda ref: [{
            "kind": "add_comment", "paragraph_ref": ref,
            "quote": "Manufacturing", "text": COMMENT_INITIAL,
        }])
        first_comments = await comments()
        _require(len(first_comments) == 1, "first comment was not created exactly once")
        first_comment = first_comments[0]
        comment_id = first_comment["id"]
        _require(first_comment["author"] == profile["author"]
                 and first_comment["initials"] == profile["initials"], "comment author profile differs")
        anchors = _comment_anchors(_own_title_insertion(_document(working), profile["author"]), comment_id)
        _require(len(anchors) == 3, "comment is not anchored inside the own pending insertion")
        await save("reedit_own_commented_revision", replacement("Manufacturing", "Production"))
        _require(await comments() == first_comments, "editing own text changed the attached comment")
        _require(_comment_anchors(_own_title_insertion(_document(working), profile["author"]), comment_id) == anchors,
                 "editing own text moved or changed comment markers")
        await save("update_own_comment", lambda ref: [{
            "kind": "update_comment", "comment_id": comment_id, "text": COMMENT_FINAL,
        }])
        await save("add_second_comment", lambda ref: [{
            "kind": "add_comment", "paragraph_ref": ref,
            "quote": "Master", "text": "Temporary acceptance comment.",
        }])
        two_comments = await comments()
        _require(len(two_comments) == 2, "second comment was not added exactly once")
        second_id = next(item["id"] for item in two_comments if item["id"] != comment_id)
        await save("delete_second_comment", lambda ref: [{"kind": "delete_comment", "comment_id": second_id}])
        final_comments = await comments()
        _require(len(final_comments) == 1 and final_comments[0]["id"] == comment_id
                 and final_comments[0]["text"] == COMMENT_FINAL, "final comments differ from the requested cycle")

        final_parts = _parts(working)
        document = etree.fromstring(final_parts["word/document.xml"])
        _require(_current_text(_title(document)) == TITLE_FINAL, "final title differs")
        settings = etree.fromstring(final_parts["word/settings.xml"])
        tracking = settings.find(_w("trackRevisions"))
        _require(tracking is not None and tracking.get(_w("val"), "true") in {"1", "true", "on"},
                 "native Word Track Changes is not enabled")
        native_comments = etree.fromstring(final_parts["word/comments.xml"])
        _require(len(native_comments) == 1 and native_comments[0].get(_w("id")) == comment_id,
                 "native comment collection differs from the MCP read result")
        _require(len(_comment_anchors(document, comment_id)) == 3, "remaining native comment anchors are incomplete")
        _require(not _comment_anchors(document, second_id), "deleted comment left native anchors behind")
        _require(len(list((output_dir / ".veqtor-backups").glob("*.docx"))) == len(steps) == 7,
                 "backup count does not match successful saves")
        _require(not list(output_dir.glob(".veqtor-*.lock")) and not list(output_dir.rglob(".veqtor-*.tmp")),
                 "publication left temporary files or writer locks")
        report = {
            "status": "passed", "working_path": str(working), "producer": producer,
            "tool_count": len(names), "tools": sorted(names), "profile": profile,
            "protocol_version": client.protocol_version,
            "save_count": len(steps), "exact_backup_count": len(backups),
            "existing_revision_count": sum(baseline.values()), "existing_revisions_preserved": True,
            "insertion_id": insertion_attributes[_w("id")], "insertion_identity_preserved": True,
            "comment_id": comment_id, "comment_anchor_preserved_on_reedit": True,
            "final_title": TITLE_FINAL, "final_comment_text": COMMENT_FINAL,
            "native_track_changes_enabled": True, "final_sha256": _sha(working.read_bytes()),
            "steps": steps,
        }
    with (output_dir / "report.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--interpreter", default=sys.executable,
                        help="Python interpreter containing the server package (default: this interpreter)")
    parser.add_argument("--author", help="override the configured tracked-change author for this smoke only")
    parser.add_argument("--initials", help="override the configured comment initials for this smoke only")
    arguments = parser.parse_args()
    report = asyncio.run(smoke(arguments.output_dir, interpreter=arguments.interpreter,
                               author=arguments.author, initials=arguments.initials))
    print(json.dumps({"status": report["status"], "producer": report["producer"],
                      "profile": report["profile"], "tool_count": report["tool_count"],
                      "save_count": report["save_count"], "working_path": report["working_path"],
                      "report_path": str(Path(report["working_path"]).parent / "report.json")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
