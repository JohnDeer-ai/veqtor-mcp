# SPDX-License-Identifier: Apache-2.0
"""Selected external text, deterministic DOCX and atomic three-member kits."""
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json
import zipfile

from lxml import etree
import pytest

from test_outgoing_package import fixture_source
from veqtor_docx import disagreement_table as table, outgoing_package as outgoing
from veqtor_docx.round_snapshots import SnapshotError


def view():
    return dict(name="Disagreements.docx", title="Таблица разногласий", language="ru",
        columns=list(table.COLUMNS), rows=[dict(sent_quote="30 дней < & >\nNext\tline",
        incoming_quote="60 дней", proposed_wording="45 дней"),
        dict(sent_quote=None, incoming_quote="Новый пункт", proposed_wording=None)])


def test_exact_plain_text_order_missing_and_minimal_package():
    selected = view()
    payload = table.render(selected)
    assert payload == table.render(deepcopy(selected))
    with zipfile.ZipFile(BytesIO(payload)) as z:
        assert set(z.namelist()) == {"[Content_Types].xml", "_rels/.rels", "word/document.xml",
                                    "word/styles.xml", "word/_rels/document.xml.rels"}
        doc = etree.fromstring(z.read("word/document.xml"))
        ns = {"w": table.W}
        rows = doc.xpath("//w:tr", namespaces=ns)
        assert len(rows) == 3
        values = []
        for cell in rows[1].xpath("./w:tc", namespaces=ns):
            text = "".join(n.text or "" if n.tag.endswith("}t") else "\n" if n.tag.endswith("}br") else "\t"
                           for n in cell.xpath(".//w:t|.//w:br|.//w:tab", namespaces=ns))
            values.append(text)
        assert values == list(selected["rows"][0].values())
        assert rows[2].xpath(".//w:t/text()", namespaces=ns) == ["Не записано", "Новый пункт", "Не записано"]
        assert not doc.xpath("//w:cantSplit|//w:ins|//w:del|//w:instrText", namespaces=ns)
        assert len(doc.xpath("//w:tblHeader", namespaces=ns)) == 1


@pytest.mark.parametrize("field,value", [("rows", []), ("rows", [{}]), ("columns", []),
    ("columns", ["proposed_wording"] * 2), ("columns", ["model_assessment"]),
    ("language", "fr"), ("language", []), ("title", ""), ("title", "x\x00y"),
    ("title", "x\ry"), ("title", "\ud800"), ("unknown", "private")])
def test_closed_bounded_input(field, value):
    selected = view()
    selected[field] = value
    with pytest.raises(SnapshotError, match="invalid_disagreement_table"):
        table.render(selected)


def test_maximum_long_rows_and_extra_private_fields():
    selected = view()
    selected["rows"] = [{c: "A" * 8000 for c in table.COLUMNS} for _ in range(20)]
    assert len(table.render(selected)) > 480000
    selected["rows"][0]["model_assessment"] = "PRIVATE"
    with pytest.raises(SnapshotError):
        table.render(selected)


def test_three_outputs_exact_report_and_no_internal_files(tmp_path, demo_dir):
    source, snap = fixture_source(tmp_path, demo_dir)
    dest = tmp_path / "kit"
    selected = view()
    plan = outgoing.prepare_outgoing_package(snap, dest, redline_name="Redline.docx",
        clean_draft_name="Clean.docx", disagreement_table=selected)
    selected["rows"][0]["proposed_wording"] = "MUTATED"
    assert json.loads(plan.report)["disagreement_table"]["view"] == view()
    assert not dest.exists()
    outgoing.publish_outgoing_package(plan, dest, expected_plan_sha256=plan.plan_sha256)
    assert {p.name for p in dest.iterdir()} == {"Redline.docx", "Clean.docx", "Disagreements.docx"}
    assert (dest / "Disagreements.docx").read_bytes() == table.render(view())
    assert source.read_bytes() == snap.payload
    assert outgoing.verify_outgoing_package(plan, dest, expected_plan_sha256=plan.plan_sha256)["status"] == "matched"


@pytest.mark.parametrize("failure", ["third_write", "forged", "name_collision"])
def test_whole_kit_refuses_without_partial_output(tmp_path, demo_dir, monkeypatch, failure):
    _, snap = fixture_source(tmp_path, demo_dir)
    dest = tmp_path / "kit"
    selected = view()
    if failure == "name_collision":
        selected["name"] = "redline.docx"
        with pytest.raises(SnapshotError):
            outgoing.prepare_outgoing_package(snap, dest, redline_name="Redline.docx", disagreement_table=selected)
    else:
        plan = outgoing.prepare_outgoing_package(snap, dest, redline_name="Redline.docx",
            clean_draft_name="Clean.docx", disagreement_table=selected)
        if failure == "forged":
            plan = replace(plan, files=(*plan.files[:2], replace(plan.files[2], payload=b"FORGED")))
        else:
            original = outgoing._write_all
            calls = 0
            def fail(fd, data):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("fictional third file write")
                return original(fd, data)
            monkeypatch.setattr(outgoing, "_write_all", fail)
        with pytest.raises(SnapshotError):
            outgoing.publish_outgoing_package(plan, dest, expected_plan_sha256=plan.plan_sha256)
    assert not dest.exists()
    assert not list(tmp_path.glob(".veqtor-outgoing-*"))
