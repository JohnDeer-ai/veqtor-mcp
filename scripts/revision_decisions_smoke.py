# SPDX-License-Identifier: Apache-2.0
"""Generate wholly fictional revision sources and independently stated outcomes.

Run with the intended installed package interpreter. The manifest's expected
properties are literal input data, never inferred from Veqtor's output. Native
Word oracle copies should start from each source, decide only its one revision
(or explicit replacement pair), then save, close and reopen.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

from veqtor_docx import drafting, generate_demo_rounds
from veqtor_docx._ooxml import W_NS, w
from veqtor_docx.revision_decisions import read_document_revisions

CASES = {
    "rPrChange": (
        "rPr",
        '<w:b/><w:color w:val="006600"/>',
        '<w:i/><w:color w:val="660000"/>',
    ),
    "pPrChange": (
        "pPr",
        '<w:tabs><w:tab w:val="left" w:pos="1440"/></w:tabs><w:jc w:val="right"/>',
        '<w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs><w:jc w:val="left"/>',
    ),
    "tcPrChange": (
        "tcPr",
        '<w:tcW w:w="2000" w:type="dxa"/><w:shd w:val="clear" w:fill="00FF00"/>',
        '<w:tcW w:w="2400" w:type="dxa"/><w:shd w:val="clear" w:fill="FFFF00"/>',
    ),
    "trPrChange": (
        "trPr",
        '<w:trHeight w:val="480" w:hRule="atLeast"/>',
        '<w:trHeight w:val="240" w:hRule="atLeast"/>',
    ),
    "tblPrChange": (
        "tblPr",
        '<w:tblW w:w="4800" w:type="dxa"/><w:jc w:val="center"/>',
        '<w:tblW w:w="5000" w:type="dxa"/><w:jc w:val="left"/>',
    ),
    "tblPrExChange": (
        "tblPrEx",
        '<w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/></w:tblCellMar>',
        '<w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="0" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="0" w:type="dxa"/></w:tblCellMar>',
    ),
    "tblGridChange": (
        "tblGrid",
        '<w:gridCol w:w="2000"/><w:gridCol w:w="2800"/>',
        '<w:gridCol w:w="2400"/><w:gridCol w:w="2400"/>',
    ),
}
PROFILE = {
    "author": "Fictional Reviewing Counsel",
    "initials": "FC",
    "producer_build": "revision-fixture.v1",
}


def xml(value):
    return etree.fromstring(f'<w:root xmlns:w="{W_NS}">{value}</w:root>')


def source_document(kind):
    root = etree.Element(w("document"), nsmap={"w": W_NS})
    body = etree.SubElement(root, w("body"))
    paragraph = etree.SubElement(body, w("p"))
    if kind in {"ins", "del", "replacement"}:
        for change, text in (
            ("del", "Previous wording."),
            ("ins", "Proposed wording."),
        ):
            if kind not in (change, "replacement"):
                continue
            revision = etree.SubElement(
                paragraph,
                w(change),
                {
                    w("id"): "20" if change == "del" else "21",
                    w("author"): "Fictional Counterparty",
                    w("date"): "2026-01-05T12:00:00Z",
                },
            )
            run = etree.SubElement(revision, w("r"))
            etree.SubElement(run, w("delText" if change == "del" else "t")).text = text
    elif kind in {"rPrChange", "pPrChange"}:
        run = etree.SubElement(paragraph, w("r"))
        etree.SubElement(run, w("t")).text = "Fictional clause."
        owner = run if kind == "rPrChange" else paragraph
    else:
        body.remove(paragraph)
        table = etree.SubElement(body, w("tbl"))
        table.append(xml('<w:tblPr><w:tblW w:w="4800" w:type="dxa"/></w:tblPr>')[0])
        table.append(
            xml(
                '<w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2800"/></w:tblGrid>'
            )[0]
        )
        row = etree.SubElement(table, w("tr"))
        cells = []
        for word in ("Fictional item", "Fictional value"):
            cell = etree.SubElement(row, w("tc"))
            cells.append(cell)
            cell.append(xml(f"<w:p><w:r><w:t>{word}</w:t></w:r></w:p>")[0])
        owner = {
            "tcPrChange": cells[0],
            "trPrChange": row,
            "tblPrExChange": row,
            "tblPrChange": table,
            "tblGridChange": table,
        }[kind]
    if kind in CASES:
        tag, current, previous = CASES[kind]
        existing = owner.find(w(tag))
        index = owner.index(existing) if existing is not None else 0
        if existing is not None:
            owner.remove(existing)
        props = xml(f"<w:{tag}>{current}</w:{tag}>")[0]
        owner.insert(index, props)
        attrs = {w("id"): "20"}
        if kind != "tblGridChange":
            attrs.update(
                {
                    w("author"): "Fictional Counterparty",
                    w("date"): "2026-01-05T12:00:00Z",
                }
            )
        change = etree.SubElement(props, w(kind), attrs)
        change.append(xml(f"<w:{tag}>{previous}</w:{tag}>")[0])
    body.append(
        xml(
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        )[0]
    )
    return root


def write_source(template, path, root):
    with (
        zipfile.ZipFile(template) as source,
        zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            payload = (
                etree.tostring(
                    root, xml_declaration=True, encoding="UTF-8", standalone=True
                )
                if item.filename == "word/document.xml"
                else source.read(item)
            )
            target.writestr(item, payload)


def generate(output):
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "revision_decision_fixtures.v1",
        "synthetic_only": True,
        "cases": [],
    }
    with tempfile.TemporaryDirectory() as temporary:
        template = generate_demo_rounds(Path(temporary) / "template")[0]
        for kind in (*CASES, "ins", "del", "replacement"):
            source = output / f"{kind}-source.docx"
            write_source(template, source, source_document(kind))
            inventory = read_document_revisions(str(source))
            refs = {row["kind"]: row["revision_ref"] for row in inventory["revisions"]}
            target = (
                {
                    "kind": "replacement",
                    "deletion_ref": refs["del"],
                    "insertion_ref": refs["ins"],
                }
                if kind == "replacement"
                else {"kind": "single", "revision_ref": refs[kind]}
            )
            case = {
                "kind": kind,
                "source": str(source),
                "sha256": inventory["file_sha256"],
                "expected": {},
                "results": {},
            }
            for decision in ("accept", "reject"):
                expected = (
                    {
                        "property_tag": CASES[kind][0],
                        "properties_xml": CASES[kind][1 if decision == "accept" else 2],
                        "text": "Fictional clause."
                        if kind in {"rPrChange", "pPrChange"}
                        else "Fictional itemFictional value",
                    }
                    if kind in CASES
                    else {
                        "text": "Proposed wording."
                        if decision == "accept" and kind in {"ins", "replacement"}
                        else "Previous wording."
                        if decision == "reject" and kind in {"del", "replacement"}
                        else ""
                    }
                )
                case["expected"][decision] = {**expected, "remaining_revisions": 0}
                destination = output / f"{kind}-{decision}-veqtor.docx"
                operations = [
                    {
                        "kind": "decide_revision",
                        "target": copy.deepcopy(target),
                        "decision": decision,
                    }
                ]
                preview = drafting.preflight_document_edits(
                    str(source),
                    inventory["file_sha256"],
                    operations,
                    output_path=str(destination),
                    **PROFILE,
                )
                if not preview["batch_applicable"]:
                    raise RuntimeError((kind, decision, preview))
                result = drafting.apply_document_edits(
                    str(source),
                    inventory["file_sha256"],
                    operations,
                    preview["preflight_proof"],
                    output_path=str(destination),
                    **PROFILE,
                )
                case["results"][decision] = result
            assert (
                hashlib.sha256(source.read_bytes()).hexdigest()
                == inventory["file_sha256"]
            )
            manifest["cases"].append(case)
    (output / "expected.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    print(
        json.dumps(
            {
                "cases": len(generate(arguments.output_dir)["cases"]),
                "output_dir": str(arguments.output_dir),
            }
        )
    )
