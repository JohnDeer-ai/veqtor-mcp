# SPDX-License-Identifier: Apache-2.0
"""Fail-closed bounds for untrusted DOCX packages and edit batches."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from veqtor_docx import (
    ApplyError,
    apply_edits,
    extract_redlines,
    list_rounds,
    preflight_edits,
    verify_quote,
)
from veqtor_docx import _ooxml
from veqtor_docx import apply as apply_module
from veqtor_docx import extract as extract_module
from veqtor_docx.apply import (
    MAX_EDIT_BATCH_SIZE,
    MAX_NEW_TEXT_CHARS_PER_BATCH,
    MAX_NEW_TEXT_CHARS_PER_EDIT,
    _validate_edit_shapes,
)
from veqtor_docx.verify import VerifyError


_DOCUMENT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Safe document</w:t></w:r></w:p></w:body>
</w:document>
"""


def _write_docx(path: Path, *extra_members: tuple[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)
        for name, payload in extra_members:
            archive.writestr(name, payload)


def _write_separated_revisions(path: Path, count: int) -> None:
    revisions = "".join(
        f'<w:ins w:id="{index}" w:author="Counterparty">'
        "<w:r><w:t>x</w:t></w:r></w:ins>"
        "<w:r><w:t> </w:t></w:r>"
        for index in range(count)
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p>'
        f"{revisions}</w:p></w:body></w:document>"
    ).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)


def _write_numbered_revision(
    path: Path,
    *,
    start: str,
    fmt: str,
    template: str = "%1.",
) -> None:
    document = '''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p>
    <w:pPr><w:outlineLvl w:val="0"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>
    <w:r><w:t>Heading </w:t></w:r>
    <w:ins w:id="1" w:author="Counterparty"><w:r><w:t>change</w:t></w:r></w:ins>
  </w:p></w:body>
</w:document>'''.encode()
    numbering = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0">
    <w:start w:val="{start}"/><w:numFmt w:val="{fmt}"/><w:lvlText w:val="{template}"/>
  </w:lvl></w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>'''.encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/numbering.xml", numbering)


def _dummy_edit(source: Path, *, insert_text: str = "") -> dict:
    return {
        "anchor": {
            "change_unit_id": "cu_001",
            "file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "delete_text": "target",
        "insert_text": insert_text,
    }


def _fail_if_member_is_decompressed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_read(*_args, **_kwargs):
        raise AssertionError("a ZIP member was decompressed before validation")

    monkeypatch.setattr(zipfile.ZipFile, "read", fail_read)


def test_public_alpha_resource_envelope_is_frozen() -> None:
    assert _ooxml.MAX_DOCX_INPUT_BYTES == 50 * 1024 * 1024
    assert _ooxml.MAX_DOCX_ZIP_MEMBERS == 2_000
    assert _ooxml.MAX_DOCX_CENTRAL_DIRECTORY_BYTES == 4 * 1024 * 1024
    assert _ooxml.MAX_DOCX_UNCOMPRESSED_BYTES == 100 * 1024 * 1024
    assert _ooxml.MAX_DOCX_XML_MEMBER_BYTES == 25 * 1024 * 1024
    assert _ooxml.MAX_DOCX_OTHER_MEMBER_BYTES == 50 * 1024 * 1024
    assert _ooxml.MAX_DOCX_XML_NODES == 100_000
    assert _ooxml.MAX_DOCX_COMPRESSION_RATIO == 200
    assert _ooxml.COMPRESSION_RATIO_MIN_UNCOMPRESSED_BYTES == 10 * 1024 * 1024
    assert extract_module.MAX_CHANGE_UNITS == 10_000
    assert extract_module.MAX_TEXT_REVISION_NESTING_DEPTH == 2
    assert extract_module.MAX_NUMBERING_LEVEL == 8
    assert MAX_EDIT_BATCH_SIZE == 100
    assert MAX_NEW_TEXT_CHARS_PER_EDIT == 20_000
    assert MAX_NEW_TEXT_CHARS_PER_BATCH == 200_000


def test_ooxml_parser_rejects_doctype_without_expanding_entities() -> None:
    payload = b'<!DOCTYPE x [<!ENTITY repeated "expanded">]><x>&repeated;</x>'

    with pytest.raises(_ooxml.DocxError, match="DOCTYPE declarations"):
        _ooxml.parse_xml(payload)


def test_xml_node_limit_is_inclusive_and_fails_before_tree_build() -> None:
    allowed = b"<root>" + b"<node/>" * 99_999 + b"</root>"
    refused = b"<root>" + b"<node/>" * 100_000 + b"</root>"

    assert len(_ooxml.parse_xml(allowed)) == 99_999
    with pytest.raises(_ooxml.ResourceLimitError) as error:
        _ooxml.parse_xml(refused)

    assert error.value.metadata == {
        "limit": "xml_node_count",
        "allowed_count": 100_000,
        "observed_count": 100_001,
        "observed_at_least": True,
    }


def test_xml_node_budget_counts_comments_processing_instructions_and_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_ooxml, "MAX_DOCX_XML_NODES", 5)

    with pytest.raises(_ooxml.ResourceLimitError) as comments_error:
        _ooxml.parse_xml(b"<root><!----><!----><!----><?x y?><child/></root>")
    assert comments_error.value.metadata["limit"] == "xml_node_count"
    assert comments_error.value.metadata["observed_count"] == 6

    with pytest.raises(_ooxml.ResourceLimitError) as attributes_error:
        _ooxml.parse_xml(b'<root a="1" b="2" c="3" d="4" e="5"/>')
    assert attributes_error.value.metadata["limit"] == "xml_node_count"
    assert attributes_error.value.metadata["observed_count"] == 6


def test_many_separated_revisions_build_one_paragraph_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "many-revisions.docx"
    _write_separated_revisions(source, 1_200)
    original = extract_module._current_paragraph_reading
    calls = 0

    def counted_reading(paragraph):
        nonlocal calls
        calls += 1
        return original(paragraph)

    monkeypatch.setattr(
        extract_module,
        "_current_paragraph_reading",
        counted_reading,
    )

    units = extract_redlines(str(source))["change_units"]

    assert len(units) == 1_200
    assert calls == 1
    assert units[0]["paragraph_context"]["before"] == ""
    assert units[0]["paragraph_context"]["after"].startswith(" x ")
    assert units[-1]["paragraph_context"]["before"].endswith("x ")
    assert units[-1]["paragraph_context"]["after"] == " "


def test_style_inheritance_is_resolved_in_linear_lookups() -> None:
    class CountingStyles(dict[str, extract_module._Style]):
        lookups = 0

        def __getitem__(self, key: str) -> extract_module._Style:
            self.lookups += 1
            return super().__getitem__(key)

    count = 6_000
    styles = CountingStyles(
        {
            f"style-{index}": extract_module._Style(
                based_on=(
                    f"style-{index + 1}" if index + 1 < count else None
                ),
                outline_lvl=(0 if index + 1 == count else None),
            )
            for index in range(count)
        }
    )

    resolved = extract_module._resolve_styles(styles)

    assert resolved["style-0"].outline_lvl == 0
    assert len(resolved) == count
    assert styles.lookups == 2 * count


def test_style_inheritance_cycle_is_refused() -> None:
    styles = {
        "a": extract_module._Style(based_on="b"),
        "b": extract_module._Style(based_on="a"),
    }

    with pytest.raises(_ooxml.DocxError, match="inheritance cycles"):
        extract_module._resolve_styles(styles)


def test_change_unit_limit_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "too-many-revisions.docx"
    _write_separated_revisions(source, 3)
    monkeypatch.setattr(extract_module, "MAX_CHANGE_UNITS", 2)

    with pytest.raises(_ooxml.ResourceLimitError) as error:
        extract_redlines(str(source))

    assert error.value.metadata["limit"] == "change_unit_count"
    assert error.value.metadata["allowed_count"] == 2
    assert error.value.metadata["observed_count"] == 3
    assert error.value.metadata["observed_at_least"] is True


def test_two_level_counter_nesting_remains_supported(tmp_path: Path) -> None:
    source = tmp_path / "counter.docx"
    document = b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body><w:p><w:ins w:id="1" w:author="Proposer">
<w:r><w:t>Proposal </w:t></w:r>
<w:del w:id="2" w:author="Reviewer"><w:r><w:delText>struck</w:delText></w:r></w:del>
</w:ins></w:p></w:body></w:document>'''
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)

    units = extract_redlines(str(source))["change_units"]

    assert [unit["change_type"] for unit in units] == ["insert", "counter"]
    assert units[0]["new_text"] == "Proposal struck"
    assert units[1]["old_text"] == "struck"


def test_three_level_text_revision_nesting_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "deep-revisions.docx"
    document = b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body><w:p><w:ins w:id="1" w:author="A"><w:del w:id="2" w:author="B">
<w:ins w:id="3" w:author="C"><w:r><w:t>x</w:t></w:r></w:ins>
</w:del></w:ins></w:p></w:body></w:document>'''
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)

    with pytest.raises(_ooxml.ResourceLimitError) as error:
        extract_redlines(str(source))

    assert error.value.metadata == {
        "limit": "text_revision_nesting_depth",
        "allowed_count": 2,
        "observed_count": 3,
        "observed_at_least": True,
        "observed_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


def test_central_directory_locator_ignores_signature_inside_zip_comment() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)
        archive.comment = b"comment containing PK\x05\x06 marker"

    _ooxml.validate_docx_central_directory(buffer.getvalue())


@pytest.mark.parametrize("fmt", ["lowerRoman", "upperRoman"])
def test_huge_roman_numbering_is_omitted_without_resource_amplification(
    tmp_path: Path,
    fmt: str,
) -> None:
    source = tmp_path / f"huge-{fmt}.docx"
    _write_numbered_revision(source, start="1" + "0" * 100, fmt=fmt)

    result = extract_redlines(str(source))

    assert len(result["change_units"]) == 1
    assert result["change_units"][0]["clause_anchor"]["label"] is None


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [("lowerRoman", "mmmcmxcix"), ("upperRoman", "MMMCMXCIX")],
)
def test_roman_numbering_support_boundary_is_explicit(
    tmp_path: Path,
    fmt: str,
    expected: str,
) -> None:
    supported = tmp_path / f"{fmt}-3999.docx"
    omitted = tmp_path / f"{fmt}-4000.docx"
    _write_numbered_revision(supported, start="3999", fmt=fmt)
    _write_numbered_revision(omitted, start="4000", fmt=fmt)

    supported_unit = extract_redlines(str(supported))["change_units"][0]
    omitted_unit = extract_redlines(str(omitted))["change_units"][0]

    assert supported_unit["clause_anchor"]["label"] == expected
    assert omitted_unit["clause_anchor"]["label"] is None


def test_oversized_numbering_template_is_omitted(tmp_path: Path) -> None:
    source = tmp_path / "long-template.docx"
    _write_numbered_revision(source, start="1", fmt="decimal", template="%1" * 129)

    unit = extract_redlines(str(source))["change_units"][0]

    assert unit["clause_anchor"]["label"] is None


def test_numbering_levels_outside_word_range_are_not_materialized() -> None:
    numbering = b'''<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:abstractNum w:abstractNumId="1">
  <w:lvl w:ilvl="8"><w:lvlText w:val="%9."/></w:lvl>
  <w:lvl w:ilvl="9"><w:lvlText w:val="%10."/></w:lvl>
</w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="1"/>
  <w:lvlOverride w:ilvl="8"/><w:lvlOverride w:ilvl="9"/>
</w:num></w:numbering>'''

    parsed = extract_module._parse_numbering(numbering)

    assert set(parsed["1"].levels) == {8}
    assert parsed["1"].overridden == {8}


def test_oversized_manual_label_is_not_repeated_across_change_units(
    tmp_path: Path,
) -> None:
    source = tmp_path / "long-manual-label.docx"
    manual_label = "1" * 257
    revisions = "".join(
        f'<w:ins w:id="{index}" w:author="Counterparty">'
        "<w:r><w:t>x</w:t></w:r></w:ins>"
        "<w:r><w:t> </w:t></w:r>"
        for index in range(3)
    )
    document = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:outlineLvl w:val="0"/></w:pPr>
      <w:r><w:t>{manual_label} Heading</w:t></w:r>
    </w:p>
    <w:p>{revisions}</w:p>
  </w:body>
</w:document>'''.encode()
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)

    units = extract_redlines(str(source))["change_units"]

    assert len(units) == 3
    assert all(
        unit["clause_anchor"] == {"label": None, "heading": "Heading"}
        for unit in units
    )


def test_input_size_limit_is_bounded_and_apply_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "never.docx"
    _write_docx(source)
    source_before = source.read_bytes()
    monkeypatch.setattr(_ooxml, "MAX_DOCX_INPUT_BYTES", len(source_before) - 1)

    with pytest.raises(_ooxml.ResourceLimitError) as extract_error:
        extract_redlines(str(source))
    assert extract_error.value.code == "resource_limit_exceeded"
    assert extract_error.value.metadata == {
        "limit": "input_docx_bytes",
        "allowed_bytes": len(source_before) - 1,
        "observed_bytes": len(source_before),
        "observed_at_least": True,
    }

    with pytest.raises(ApplyError) as apply_error:
        apply_edits(str(source), str(output), [_dummy_edit(source)])
    assert apply_error.value.code == "resource_limit_exceeded"
    assert apply_error.value.metadata["limit"] == "input_docx_bytes"
    assert apply_error.value.metadata["failure_phase"] == "source"
    assert source.read_bytes() == source_before
    assert not output.exists()
    assert not list(tmp_path.glob("*.veqtor-tmp"))


def test_zip_member_count_is_rejected_before_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "members.docx"
    _write_docx(source, ("one.bin", b"1"), ("two.bin", b"2"))
    monkeypatch.setattr(_ooxml, "MAX_DOCX_ZIP_MEMBERS", 2)
    _fail_if_member_is_decompressed(monkeypatch)

    with pytest.raises(_ooxml.ResourceLimitError) as error:
        extract_redlines(str(source))

    assert error.value.metadata["limit"] == "zip_member_count"
    assert error.value.metadata["allowed_count"] == 2
    assert error.value.metadata["observed_count"] == 3


def test_duplicate_zip_members_are_rejected_consistently_before_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "duplicate.docx"
    output = tmp_path / "never.docx"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("word/document.xml", _DOCUMENT_XML)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    edit = _dummy_edit(source)
    _fail_if_member_is_decompressed(monkeypatch)

    with pytest.raises(_ooxml.DocxError) as extract_error:
        extract_redlines(str(source))
    assert extract_error.value.code == "file_unextractable"
    assert extract_error.value.metadata == {
        "observed_source_sha256": source_sha
    }

    rounds = list_rounds(str(tmp_path))
    assert rounds["rounds"] == []
    assert rounds["skipped"] == [
        {"filename": source.name, "reason": "invalid_docx"}
    ]

    with pytest.raises(VerifyError) as verify_error:
        verify_quote(
            str(source),
            {"change_unit_id": "cu_001", "file_sha256": source_sha},
            "Safe document",
        )
    assert verify_error.value.code == "file_unextractable"
    assert verify_error.value.metadata == {
        "claimed_source_sha256": source_sha,
        "observed_source_sha256": source_sha,
    }

    preflight = preflight_edits(str(source), [edit])
    assert preflight["batch_applicable"] is False
    assert preflight["refusal_code"] == "file_unextractable"
    assert preflight["failure_phase"] == "source"

    with pytest.raises(_ooxml.DocxError) as apply_error:
        apply_edits(str(source), str(output), [edit])
    assert apply_error.value.code == "file_unextractable"
    assert apply_error.value.metadata == {
        "observed_source_sha256": source_sha,
        "failure_phase": "source",
    }
    assert not output.exists()


def test_zip_member_count_is_rejected_before_zipinfo_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "members-preflight.docx"
    _write_docx(source, ("one.bin", b"1"), ("two.bin", b"2"))
    monkeypatch.setattr(_ooxml, "MAX_DOCX_ZIP_MEMBERS", 2)

    def fail_open(*_args, **_kwargs):
        raise AssertionError("ZipFile opened before central-directory preflight")

    monkeypatch.setattr(zipfile.ZipFile, "__init__", fail_open)
    with pytest.raises(_ooxml.ResourceLimitError) as error:
        extract_redlines(str(source))

    assert error.value.metadata["limit"] == "zip_member_count"
    assert error.value.metadata["observed_count"] == 3


def test_central_directory_size_is_rejected_before_zipinfo_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "large-directory.docx"
    _write_docx(source)
    monkeypatch.setattr(_ooxml, "MAX_DOCX_CENTRAL_DIRECTORY_BYTES", 1)

    def fail_open(*_args, **_kwargs):
        raise AssertionError("ZipFile opened before central-directory preflight")

    monkeypatch.setattr(zipfile.ZipFile, "__init__", fail_open)
    with pytest.raises(_ooxml.ResourceLimitError) as error:
        extract_redlines(str(source))

    assert error.value.metadata["limit"] == "central_directory_bytes"
    assert error.value.metadata["allowed_bytes"] == 1
    assert error.value.metadata["observed_bytes"] > 1


def test_xml_member_size_is_rejected_before_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "xml-member.docx"
    _write_docx(source)
    monkeypatch.setattr(
        _ooxml, "MAX_DOCX_XML_MEMBER_BYTES", len(_DOCUMENT_XML) - 1
    )
    _fail_if_member_is_decompressed(monkeypatch)

    with pytest.raises(_ooxml.ResourceLimitError) as error:
        extract_redlines(str(source))

    assert error.value.metadata["limit"] == "xml_member_bytes"
    assert error.value.metadata["member_name"] == "word/document.xml"
    assert error.value.metadata["observed_bytes"] == len(_DOCUMENT_XML)


def test_other_member_size_is_rejected_before_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "other-member.docx"
    _write_docx(source, ("word/media/blob.bin", b"x" * 101))
    monkeypatch.setattr(_ooxml, "MAX_DOCX_OTHER_MEMBER_BYTES", 100)
    _fail_if_member_is_decompressed(monkeypatch)

    with pytest.raises(_ooxml.ResourceLimitError) as error:
        extract_redlines(str(source))

    assert error.value.metadata["limit"] == "other_member_bytes"
    assert error.value.metadata["member_name"] == "word/media/blob.bin"
    assert error.value.metadata["allowed_bytes"] == 100
    assert error.value.metadata["observed_bytes"] == 101


def test_total_uncompressed_size_is_rejected_before_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extra = b"not-highly-compressible"
    source = tmp_path / "total.docx"
    _write_docx(source, ("custom/data.bin", extra))
    observed = len(_DOCUMENT_XML) + len(extra)
    monkeypatch.setattr(_ooxml, "MAX_DOCX_UNCOMPRESSED_BYTES", observed - 1)
    _fail_if_member_is_decompressed(monkeypatch)

    with pytest.raises(_ooxml.ResourceLimitError) as error:
        extract_redlines(str(source))

    assert error.value.metadata["limit"] == "total_uncompressed_bytes"
    assert error.value.metadata["allowed_bytes"] == observed - 1
    assert error.value.metadata["observed_bytes"] == observed


def test_high_compression_ratio_is_rejected_before_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "ratio.docx"
    _write_docx(source, ("custom/repeated.bin", b"A" * 5_000))
    monkeypatch.setattr(
        _ooxml, "COMPRESSION_RATIO_MIN_UNCOMPRESSED_BYTES", 1_000
    )
    monkeypatch.setattr(_ooxml, "MAX_DOCX_COMPRESSION_RATIO", 10)
    _fail_if_member_is_decompressed(monkeypatch)

    with pytest.raises(_ooxml.ResourceLimitError) as error:
        extract_redlines(str(source))

    assert error.value.metadata["limit"] == "compression_ratio"
    assert error.value.metadata["member_name"] == "custom/repeated.bin"
    assert error.value.metadata["allowed_ratio"] == 10
    assert error.value.metadata["observed_ratio"] > 10


def test_round_listing_skips_a_resource_bounded_docx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "oversized.docx"
    _write_docx(source, ("extra.bin", b"x"))
    monkeypatch.setattr(_ooxml, "MAX_DOCX_ZIP_MEMBERS", 1)
    _fail_if_member_is_decompressed(monkeypatch)

    result = list_rounds(str(tmp_path))

    assert result["rounds"] == []
    assert result["skipped"] == [
        {"filename": source.name, "reason": "resource_limit_exceeded"}
    ]


def test_round_listing_preserves_xml_element_limit_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "element-heavy.docx"
    _write_docx(source)
    monkeypatch.setattr(_ooxml, "MAX_DOCX_XML_NODES", 5)

    result = list_rounds(str(tmp_path))

    assert result["rounds"] == []
    assert result["skipped"] == [
        {"filename": source.name, "reason": "resource_limit_exceeded"}
    ]


def test_verify_preserves_the_resource_refusal_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "verify.docx"
    _write_docx(source, ("extra.bin", b"x"))
    monkeypatch.setattr(_ooxml, "MAX_DOCX_ZIP_MEMBERS", 1)

    with pytest.raises(VerifyError) as error:
        verify_quote(
            str(source),
            {"change_unit_id": "cu_001", "file_sha256": "0" * 64},
            "quote",
        )

    assert error.value.code == "resource_limit_exceeded"
    assert error.value.metadata["limit"] == "zip_member_count"
    assert str(error.value).count("resource_limit_exceeded") == 1


def test_generated_candidate_must_remain_inside_readable_envelope(
    demo_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    baseline_output = tmp_path / "baseline.docx"
    output = tmp_path / "never.docx"
    extraction = extract_redlines(str(source))
    unit = next(
        item
        for item in extraction["change_units"]
        if item["clause_anchor"] and item["clause_anchor"]["label"] == "14.2"
    )
    edit = {
        "anchor": {
            "change_unit_id": unit["change_unit_id"],
            "file_sha256": extraction["file_sha256"],
        },
        "delete_text": " in respect of all claims in aggregate.",
        "insert_text": " in respect of all claims arising in any Contract Year.",
    }
    apply_edits(str(source), str(baseline_output), [edit])
    assert baseline_output.stat().st_size > source.stat().st_size

    monkeypatch.setattr(_ooxml, "MAX_DOCX_INPUT_BYTES", source.stat().st_size)
    preflight = preflight_edits(str(source), [edit])
    assert preflight["batch_applicable"] is False
    assert preflight["refusal_code"] == "resource_limit_exceeded"
    assert preflight["failure_phase"] == "round_trip"
    assert "candidate_docx_bytes" in preflight["reason"]

    with pytest.raises(_ooxml.ResourceLimitError) as error:
        apply_edits(str(source), str(output), [edit])
    assert error.value.metadata["limit"] == "candidate_docx_bytes"
    assert error.value.metadata["observed_candidate_sha256"]
    assert not output.exists()


def test_archive_limit_returns_structured_preflight_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "never.docx"
    _write_docx(source, ("extra.bin", b"x"))
    source_before = source.read_bytes()
    edit = _dummy_edit(source)
    monkeypatch.setattr(_ooxml, "MAX_DOCX_ZIP_MEMBERS", 1)
    _fail_if_member_is_decompressed(monkeypatch)

    preflight = preflight_edits(str(source), [edit])
    assert preflight["batch_applicable"] is False
    assert preflight["refusal_code"] == "resource_limit_exceeded"
    assert preflight["failure_phase"] == "source"
    assert "zip_member_count" in preflight["reason"]

    with pytest.raises(_ooxml.ResourceLimitError) as apply_error:
        apply_edits(str(source), str(output), [edit])
    assert apply_error.value.metadata["limit"] == "zip_member_count"
    assert apply_error.value.metadata["observed_source_sha256"] == hashlib.sha256(
        source_before
    ).hexdigest()
    assert source.read_bytes() == source_before
    assert not output.exists()


@pytest.mark.parametrize("field", ["insert_text", "reinstate_text"])
def test_per_edit_new_text_limit_is_structured_and_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "never.docx"
    _write_docx(source)
    source_before = source.read_bytes()
    edit = _dummy_edit(source)
    if field == "reinstate_text":
        edit = {"anchor": edit["anchor"], "reinstate_text": "x" * 20_001}
    else:
        edit[field] = "x" * 20_001
    _fail_if_member_is_decompressed(monkeypatch)

    preflight = preflight_edits(str(source), [edit])
    assert preflight["batch_applicable"] is False
    assert preflight["blocking_edit_index"] == 0
    assert preflight["refusal_code"] == "resource_limit_exceeded"
    assert preflight["failure_phase"] == "validation"

    with pytest.raises(ApplyError) as apply_error:
        apply_edits(str(source), str(output), [edit])
    assert apply_error.value.metadata["limit"] == "new_text_chars_per_edit"
    assert apply_error.value.metadata["allowed_chars"] == 20_000
    assert apply_error.value.metadata["observed_chars"] == 20_001
    assert source.read_bytes() == source_before
    assert not output.exists()


def test_edit_count_limit_is_structured_and_checked_before_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "never.docx"
    _write_docx(source)
    source_before = source.read_bytes()
    edits = [_dummy_edit(source) for _ in range(101)]
    _fail_if_member_is_decompressed(monkeypatch)

    preflight = preflight_edits(str(source), edits)
    assert preflight["batch_applicable"] is False
    assert preflight["refusal_code"] == "resource_limit_exceeded"
    assert preflight["failure_phase"] == "validation"

    with pytest.raises(ApplyError) as apply_error:
        apply_edits(str(source), str(output), edits)
    assert apply_error.value.metadata["limit"] == "edit_count"
    assert apply_error.value.metadata["allowed_count"] == 100
    assert apply_error.value.metadata["observed_count"] == 101
    assert source.read_bytes() == source_before
    assert not output.exists()


def test_total_new_text_limit_is_structured_and_checked_before_decompression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "never.docx"
    _write_docx(source)
    source_before = source.read_bytes()
    edits = [_dummy_edit(source, insert_text="x" * 2_001) for _ in range(100)]
    _fail_if_member_is_decompressed(monkeypatch)

    preflight = preflight_edits(str(source), edits)
    assert preflight["batch_applicable"] is False
    assert preflight["blocking_edit_index"] == 99
    assert preflight["refusal_code"] == "resource_limit_exceeded"
    assert preflight["failure_phase"] == "validation"

    with pytest.raises(ApplyError) as apply_error:
        apply_edits(str(source), str(output), edits)
    assert apply_error.value.metadata["limit"] == "new_text_chars_per_batch"
    assert apply_error.value.metadata["allowed_chars"] == 200_000
    assert apply_error.value.metadata["observed_chars"] == 200_100
    assert source.read_bytes() == source_before
    assert not output.exists()


def test_edit_limits_are_inclusive() -> None:
    anchor = {"change_unit_id": "cu_001", "file_sha256": "0" * 64}
    edits = [
        {
            "anchor": anchor,
            "delete_text": "target",
            "insert_text": "x" * 20_000,
        }
        for _ in range(10)
    ]

    _validate_edit_shapes(edits)


def test_ambiguous_delete_matching_stops_after_second_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ObservedReading(str):
        find_calls = 0

        def find(self, sub, start=None, end=None):
            self.find_calls += 1
            if start is None:
                return super().find(sub)
            if end is None:
                return super().find(sub, start)
            return super().find(sub, start, end)

    reading = ObservedReading("a" * 1_000_000)
    monkeypatch.setattr(apply_module, "_paragraph_segments", lambda _para: [])
    monkeypatch.setattr(apply_module, "_reading_text", lambda _segments: reading)

    with pytest.raises(ApplyError) as error:
        apply_module._match_delete_span(etree.Element("p"), "a", "Author")

    assert error.value.code == "delete_text_ambiguous"
    assert error.value.metadata["match_count"] == 2
    assert reading.find_calls == 2
