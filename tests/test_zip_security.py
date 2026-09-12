# SPDX-License-Identifier: Apache-2.0
"""Adversarial ZIP boundaries shared by every public DOCX operation."""

from __future__ import annotations

from collections import Counter
import hashlib
import io
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from veqtor_docx import (
    DocxError,
    VerifyError,
    apply_edits,
    extract_redlines,
    list_rounds,
    preflight_edits,
    verify_quote,
)
from veqtor_docx import _ooxml
from veqtor_docx import apply as apply_module
from veqtor_docx import extract as extract_module


_DOCUMENT_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:ins w:id="1" w:author="Counterparty">
    <w:r><w:t>Safe change</w:t></w:r>
  </w:ins></w:p></w:body>
</w:document>
'''
_AUTO = object()
_SURFACES = ("list", "extract", "verify", "preflight", "apply")


def _raw_deflate(payload: bytes) -> bytes:
    compressor = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    return compressor.compress(payload) + compressor.flush()


def _value(value: object, default: int) -> int:
    return default if value is _AUTO else int(value)


def _single_member_docx(
    content: bytes = _DOCUMENT_XML,
    *,
    compressed: bytes | None = None,
    method: int = zipfile.ZIP_DEFLATED,
    local_method: int | None = None,
    flags: int | None = None,
    local_flags: int | None = None,
    descriptor: str | None = None,
    local_name: bytes | None = None,
    central_crc: object = _AUTO,
    local_crc: object = _AUTO,
    central_compressed_size: object = _AUTO,
    local_compressed_size: object = _AUTO,
    central_file_size: object = _AUTO,
    local_file_size: object = _AUTO,
    descriptor_crc: object = _AUTO,
    descriptor_compressed_size: object = _AUTO,
    descriptor_file_size: object = _AUTO,
    local_extra: bytes = b"",
    central_extra: bytes = b"",
) -> bytes:
    name = b"word/document.xml"
    local_name = name if local_name is None else local_name
    if compressed is None:
        compressed = content if method == zipfile.ZIP_STORED else _raw_deflate(content)
    actual_crc = zlib.crc32(content) & 0xFFFFFFFF
    central_crc_value = _value(central_crc, actual_crc)
    central_compressed = _value(central_compressed_size, len(compressed))
    central_file = _value(central_file_size, len(content))
    if flags is None:
        flags = 0x0008 if descriptor is not None else 0
    if local_flags is None:
        local_flags = flags
    if local_method is None:
        local_method = method

    if descriptor is None:
        local_crc_value = _value(local_crc, central_crc_value)
        local_compressed = _value(local_compressed_size, central_compressed)
        local_file = _value(local_file_size, central_file)
    else:
        local_crc_value = _value(local_crc, 0)
        local_compressed = _value(local_compressed_size, 0)
        local_file = _value(local_file_size, 0)

    local = struct.pack(
        "<4s5H3L2H",
        b"PK\x03\x04",
        20,
        local_flags,
        local_method,
        0,
        0,
        local_crc_value,
        local_compressed,
        local_file,
        len(local_name),
        len(local_extra),
    ) + local_name + local_extra

    descriptor_payload = b""
    if descriptor is not None:
        descriptor_values = struct.pack(
            "<3L",
            _value(descriptor_crc, central_crc_value),
            _value(descriptor_compressed_size, central_compressed),
            _value(descriptor_file_size, central_file),
        )
        if descriptor == "signed":
            descriptor_payload = b"PK\x07\x08" + descriptor_values
        elif descriptor == "unsigned":
            descriptor_payload = descriptor_values
        else:
            raise AssertionError(f"unknown descriptor form: {descriptor}")

    central_offset = len(local) + len(compressed) + len(descriptor_payload)
    central = struct.pack(
        "<4s6H3L5H2L",
        b"PK\x01\x02",
        20,
        20,
        flags,
        method,
        0,
        0,
        central_crc_value,
        central_compressed,
        central_file,
        len(name),
        len(central_extra),
        0,
        0,
        0,
        0,
        0,
    ) + name + central_extra
    eocd = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        1,
        1,
        len(central),
        central_offset,
        0,
    )
    return local + compressed + descriptor_payload + central + eocd


def _anchor(source: Path) -> dict[str, str]:
    return {
        "change_unit_id": "cu_001",
        "file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


def _edit(source: Path) -> dict:
    return {
        "anchor": _anchor(source),
        "delete_text": "Safe change",
        "insert_text": "Safer change",
    }


def _assert_surface_refusal(
    surface: str,
    source: Path,
    tmp_path: Path,
    *,
    expected_code: str,
    expected_list_reason: str,
) -> None:
    output = tmp_path / f"{surface}-never.docx"
    if surface == "list":
        result = list_rounds(str(tmp_path))
        assert result["rounds"] == []
        assert result["skipped"] == [
            {"filename": source.name, "reason": expected_list_reason}
        ]
    elif surface == "extract":
        with pytest.raises(DocxError) as error:
            extract_redlines(str(source))
        assert getattr(error.value, "code", None) == expected_code
    elif surface == "verify":
        with pytest.raises(VerifyError) as error:
            verify_quote(str(source), _anchor(source), "Safe change")
        assert error.value.code == expected_code
    elif surface == "preflight":
        result = preflight_edits(str(source), [_edit(source)])
        assert result["batch_applicable"] is False
        assert result["refusal_code"] == expected_code
        assert result["failure_phase"] == "source"
    elif surface == "apply":
        with pytest.raises(DocxError) as error:
            apply_edits(str(source), str(output), [_edit(source)])
        assert getattr(error.value, "code", None) == expected_code
    else:
        raise AssertionError(f"unknown surface: {surface}")
    assert not output.exists()
    assert not list(tmp_path.glob("*.veqtor-tmp"))


@pytest.mark.parametrize("surface", _SURFACES)
def test_forged_declared_size_is_rejected_on_every_surface(
    surface: str,
    tmp_path: Path,
) -> None:
    actual = _DOCUMENT_XML + b" " * 4096
    forged = _single_member_docx(
        actual,
        central_crc=zlib.crc32(_DOCUMENT_XML) & 0xFFFFFFFF,
        local_crc=zlib.crc32(_DOCUMENT_XML) & 0xFFFFFFFF,
        central_file_size=len(_DOCUMENT_XML),
        local_file_size=len(_DOCUMENT_XML),
    )
    source = tmp_path / "forged.docx"
    source.write_bytes(forged)

    _assert_surface_refusal(
        surface,
        source,
        tmp_path,
        expected_code="file_unextractable",
        expected_list_reason="invalid_docx",
    )


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("method", [12, 14, 93, 99])
def test_unsupported_compression_is_rejected_before_any_decoder(
    method: int,
    surface: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / f"method-{method}.docx"
    source.write_bytes(_single_member_docx(method=method))

    def fail_decoder(*_args, **_kwargs):
        raise AssertionError("decoder created for a forbidden ZIP method")

    monkeypatch.setattr(_ooxml.zlib, "decompressobj", fail_decoder)
    _assert_surface_refusal(
        surface,
        source,
        tmp_path,
        expected_code="unsupported_compression",
        expected_list_reason="unsupported_compression",
    )


def test_hostile_lzma_dictionary_is_rejected_before_lzma_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_LZMA) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)
    payload = bytearray(buffer.getvalue())
    local_offset = payload.index(b"PK\x03\x04")
    name_size, extra_size = struct.unpack_from("<HH", payload, local_offset + 26)
    data_offset = local_offset + 30 + name_size + extra_size
    payload[data_offset + 5 : data_offset + 9] = b"\xff\xff\xff\xff"
    source = tmp_path / "hostile-lzma.docx"
    source.write_bytes(payload)
    decoder_created = False

    def fail_lzma_decoder(*_args, **_kwargs):
        nonlocal decoder_created
        decoder_created = True
        raise AssertionError("LZMA decoder must not be constructed")

    monkeypatch.setattr(zipfile, "LZMADecompressor", fail_lzma_decoder)
    with pytest.raises(DocxError) as error:
        extract_redlines(str(source))

    assert getattr(error.value, "code", None) == "unsupported_compression"
    assert decoder_created is False


@pytest.mark.parametrize("descriptor", ["signed", "unsigned"])
def test_standard_data_descriptors_work_across_all_surfaces(
    descriptor: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / f"descriptor-{descriptor}.docx"
    output = tmp_path / f"descriptor-{descriptor}-output.docx"
    source.write_bytes(_single_member_docx(descriptor=descriptor))

    extracted = extract_redlines(str(source))
    unit = extracted["change_units"][0]
    anchor = {
        "change_unit_id": unit["change_unit_id"],
        "file_sha256": extracted["file_sha256"],
    }
    edit = {
        "anchor": anchor,
        "delete_text": "Safe change",
        "insert_text": "Safer change",
    }

    rounds = list_rounds(str(tmp_path))
    assert len(rounds["rounds"]) == 1
    assert rounds["skipped"] == []
    assert verify_quote(str(source), anchor, "Safe change")["verdict"] == "exact"
    assert preflight_edits(str(source), [edit])["batch_applicable"] is True
    assert apply_edits(str(source), str(output), [edit])["status"] == "ok"
    assert output.exists()


def _wrong_crc() -> bytes:
    return _single_member_docx(central_crc=0, local_crc=0)


def _local_size_mismatch() -> bytes:
    return _single_member_docx(local_file_size=len(_DOCUMENT_XML) - 1)


def _local_compressed_size_mismatch() -> bytes:
    compressed = _raw_deflate(_DOCUMENT_XML)
    return _single_member_docx(
        compressed=compressed,
        local_compressed_size=len(compressed) - 1,
    )


def _local_method_mismatch() -> bytes:
    return _single_member_docx(local_method=zipfile.ZIP_STORED)


def _local_flags_mismatch() -> bytes:
    return _single_member_docx(local_flags=0x0008)


def _local_name_mismatch() -> bytes:
    return _single_member_docx(local_name=b"word/other.xml")


def _short_compressed_span() -> bytes:
    compressed = _raw_deflate(_DOCUMENT_XML)
    return _single_member_docx(
        compressed=compressed,
        central_compressed_size=len(compressed) - 1,
        local_compressed_size=len(compressed) - 1,
    )


def _long_compressed_span() -> bytes:
    compressed = _raw_deflate(_DOCUMENT_XML)
    return _single_member_docx(
        compressed=compressed,
        central_compressed_size=len(compressed) + 1,
        local_compressed_size=len(compressed) + 1,
    )


def _truncated_deflate() -> bytes:
    compressed = _raw_deflate(_DOCUMENT_XML)[:-1]
    return _single_member_docx(compressed=compressed)


def _trailing_deflate() -> bytes:
    compressed = _raw_deflate(_DOCUMENT_XML) + b"trailing"
    return _single_member_docx(compressed=compressed)


def _bad_descriptor() -> bytes:
    return _single_member_docx(descriptor="signed", descriptor_crc=0)


def _bad_descriptor_compressed_size() -> bytes:
    return _single_member_docx(
        descriptor="signed",
        descriptor_compressed_size=0,
    )


def _bad_descriptor_file_size() -> bytes:
    return _single_member_docx(descriptor="signed", descriptor_file_size=0)


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize(
    "payload_factory",
    [
        _wrong_crc,
        _local_size_mismatch,
        _local_compressed_size_mismatch,
        _local_method_mismatch,
        _local_flags_mismatch,
        _local_name_mismatch,
        _short_compressed_span,
        _long_compressed_span,
        _truncated_deflate,
        _trailing_deflate,
        _bad_descriptor,
        _bad_descriptor_compressed_size,
        _bad_descriptor_file_size,
    ],
    ids=[
        "crc",
        "local-size",
        "local-compressed-size",
        "local-method",
        "local-flags",
        "local-name",
        "short-compressed-span",
        "long-compressed-span",
        "truncated-deflate",
        "trailing-deflate",
        "descriptor-crc",
        "descriptor-compressed-size",
        "descriptor-file-size",
    ],
)
def test_corrupt_zip_layout_is_rejected_on_every_surface(
    payload_factory,
    surface: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "corrupt.docx"
    source.write_bytes(payload_factory())

    _assert_surface_refusal(
        surface,
        source,
        tmp_path,
        expected_code="file_unextractable",
        expected_list_reason="invalid_docx",
    )


@pytest.mark.parametrize(
    "payload_factory",
    [_wrong_crc, _truncated_deflate, _trailing_deflate],
    ids=["crc", "truncated", "trailing"],
)
def test_late_decode_refusals_keep_their_expanded_output_charged(
    payload_factory,
) -> None:
    budget = _ooxml.ExpandedOutputBudget(
        allowed_bytes=len(_DOCUMENT_XML),
        limit="test_expanded_bytes",
    )

    with pytest.raises(DocxError):
        _ooxml.load_validated_docx(
            payload_factory(),
            capture=(_ooxml.DOCUMENT_PART,),
            expanded_budget=budget,
        )

    assert budget.consumed_bytes == len(_DOCUMENT_XML)


def test_forged_size_refusal_charges_the_first_extra_output_byte() -> None:
    actual = _DOCUMENT_XML + b"extra output"
    declared_crc = zlib.crc32(_DOCUMENT_XML) & 0xFFFFFFFF
    payload = _single_member_docx(
        actual,
        central_crc=declared_crc,
        local_crc=declared_crc,
        central_file_size=len(_DOCUMENT_XML),
        local_file_size=len(_DOCUMENT_XML),
    )
    budget = _ooxml.ExpandedOutputBudget(
        allowed_bytes=len(actual),
        limit="test_expanded_bytes",
    )

    with pytest.raises(DocxError):
        _ooxml.load_validated_docx(
            payload,
            capture=(_ooxml.DOCUMENT_PART,),
            expanded_budget=budget,
        )

    assert budget.consumed_bytes == len(_DOCUMENT_XML) + 1


def test_multichunk_deflate_refuses_at_the_first_byte_over_shared_budget() -> None:
    content = b"x" * (3 * _ooxml.ZIP_DECODE_CHUNK_BYTES)
    allowed = _ooxml.ZIP_DECODE_CHUNK_BYTES + 17
    budget = _ooxml.ExpandedOutputBudget(
        allowed_bytes=allowed,
        limit="test_expanded_bytes",
    )

    with pytest.raises(_ooxml.ExpandedOutputBudgetExceeded) as error:
        _ooxml.load_validated_docx(
            _single_member_docx(content),
            capture=(_ooxml.DOCUMENT_PART,),
            expanded_budget=budget,
        )

    assert budget.consumed_bytes == allowed + 1
    assert error.value.metadata["observed_bytes"] == allowed + 1


def test_predecode_refusal_does_not_charge_expanded_output() -> None:
    budget = _ooxml.ExpandedOutputBudget(
        allowed_bytes=0,
        limit="test_expanded_bytes",
    )

    with pytest.raises(_ooxml.UnsupportedCompressionError):
        _ooxml.load_validated_docx(
            _single_member_docx(method=zipfile.ZIP_LZMA),
            capture=(_ooxml.DOCUMENT_PART,),
            expanded_budget=budget,
        )

    assert budget.consumed_bytes == 0


def test_uncaptured_members_are_charged_to_the_expanded_output_budget() -> None:
    extra = b"uncaptured member output"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_ooxml.DOCUMENT_PART, _DOCUMENT_XML)
        archive.writestr("custom/uncaptured.bin", extra)
    expected = len(_DOCUMENT_XML) + len(extra)
    budget = _ooxml.ExpandedOutputBudget(
        allowed_bytes=expected,
        limit="test_expanded_bytes",
    )

    package = _ooxml.load_validated_docx(
        buffer.getvalue(),
        capture=(_ooxml.DOCUMENT_PART,),
        expanded_budget=budget,
    )

    assert set(package.parts) == {_ooxml.DOCUMENT_PART}
    assert package.expanded_bytes == expected
    assert budget.consumed_bytes == expected


def test_stored_member_budget_refuses_before_crc_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _single_member_docx(method=zipfile.ZIP_STORED)
    budget = _ooxml.ExpandedOutputBudget(
        allowed_bytes=len(_DOCUMENT_XML) - 1,
        limit="test_expanded_bytes",
    )

    def fail_crc(*_args, **_kwargs):
        raise AssertionError("STORED CRC ran after aggregate budget exhaustion")

    monkeypatch.setattr(_ooxml.zlib, "crc32", fail_crc)
    with pytest.raises(_ooxml.ExpandedOutputBudgetExceeded) as error:
        _ooxml.load_validated_docx(
            payload,
            capture=(_ooxml.DOCUMENT_PART,),
            expanded_budget=budget,
        )

    assert error.value.metadata["observed_bytes"] == len(_DOCUMENT_XML)
    assert budget.consumed_bytes == len(_DOCUMENT_XML)


@pytest.mark.parametrize("extra_location", ["local", "central"])
def test_member_level_zip64_extra_is_rejected(
    extra_location: str,
    tmp_path: Path,
) -> None:
    zip64_extra = struct.pack("<HH", 0x0001, 0)
    kwargs = {f"{extra_location}_extra": zip64_extra}
    source = tmp_path / f"zip64-{extra_location}.docx"
    source.write_bytes(_single_member_docx(**kwargs))

    with pytest.raises(DocxError) as error:
        extract_redlines(str(source))
    assert getattr(error.value, "code", None) == "file_unextractable"


def test_product_paths_do_not_use_zipfile_member_readers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.docx"
    output = tmp_path / "output.docx"
    source.write_bytes(_single_member_docx())
    original_open = zipfile.ZipFile.open

    def fail_read(*_args, **_kwargs):
        raise AssertionError("production input path called ZipFile.read")

    def guard_open(self, name, mode="r", pwd=None, *, force_zip64=False):
        if mode == "r":
            raise AssertionError("production input path called ZipFile.open for read")
        return original_open(
            self,
            name,
            mode=mode,
            pwd=pwd,
            force_zip64=force_zip64,
        )

    monkeypatch.setattr(zipfile.ZipFile, "read", fail_read)
    monkeypatch.setattr(zipfile.ZipFile, "open", guard_open)

    extracted = extract_redlines(str(source))
    unit = extracted["change_units"][0]
    anchor = {
        "change_unit_id": unit["change_unit_id"],
        "file_sha256": extracted["file_sha256"],
    }
    edit = {
        "anchor": anchor,
        "delete_text": "Safe change",
        "insert_text": "Safer change",
    }
    assert len(list_rounds(str(tmp_path))["rounds"]) == 1
    assert verify_quote(str(source), anchor, "Safe change")["verdict"] == "exact"
    assert preflight_edits(str(source), [edit])["batch_applicable"] is True
    assert apply_edits(str(source), str(output), [edit])["status"] == "ok"


@pytest.mark.parametrize("scenario", ["legacy", "paragraph", "mixed"])
@pytest.mark.parametrize("operation", ["apply", "preflight"])
def test_apply_validates_source_once_and_candidate_once(tmp_path, monkeypatch, scenario, operation):
    # Count the real central-directory and member-decoding boundaries, whichever
    # module alias called the loader. Roles follow the captured/serialized bytes.
    from veqtor_docx import inspect as inspect_module, inspect_document
    from veqtor_docx.synthetic import generate_demo_rounds
    from test_paragraph_fix2 import mixed_edits

    source = tmp_path / "source.docx"
    if scenario == "legacy":
        source.write_bytes(_single_member_docx())
    else:
        source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[scenario == "mixed"]
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("custom/stored.bin", b"Retain and validate this opaque member", compress_type=zipfile.ZIP_STORED)
        archive.writestr("custom/deflated.bin", b"Validate this too" * 100, compress_type=zipfile.ZIP_DEFLATED)
    before = source.read_bytes()
    source_sha = hashlib.sha256(before).hexdigest()
    with zipfile.ZipFile(source) as archive:
        members = set(archive.namelist())
        expanded = sum(info.file_size for info in archive.infolist())
    if scenario == "legacy":
        unit = extract_redlines(str(source))["change_units"][0]
        edits = [{"anchor": unit["anchor"], "delete_text": "Safe change", "insert_text": "Safer change"}]
    elif scenario == "mixed":
        edits = mixed_edits(source)
    else:
        ref = inspect_document(str(source), mode="browse")["paragraphs"][0]["paragraph_ref"]
        edits = [{"target": {"kind": "paragraph", "paragraph_ref": ref},
                  "delete_text": "30 days", "insert_text": "45 days"}]
    build = "validation-reuse-regression"
    proof = preflight_edits(str(source), edits, producer_build=build)["preflight_proof"]
    assert proof is not None
    reader = apply_module.read_docx_payload
    serializer = apply_module._output_archive_bytes
    validate = _ooxml.validate_docx_central_directory
    reads, candidates, validations, decodes = [], [], [], []

    def observe_read(path):
        payload = reader(path)
        reads.append((str(path), payload))
        return payload

    def observe_serialize(*args, **kwargs):
        payload = serializer(*args, **kwargs)
        candidates.append(payload)
        return payload

    def observe_validation(payload):
        validations.append(payload)
        return validate(payload)

    def observe_decoder(decode):
        def observed(payload, entry, *args, **kwargs):
            result = decode(payload, entry, *args, **kwargs)
            decodes.append((payload, entry.filename, result[1], kwargs["capture"]))
            return result
        return observed

    for module in (apply_module, extract_module, inspect_module, _ooxml):
        monkeypatch.setattr(module, "read_docx_payload", observe_read)
    monkeypatch.setattr(apply_module, "_output_archive_bytes", observe_serialize)
    monkeypatch.setattr(_ooxml, "validate_docx_central_directory", observe_validation)
    for name in ("_decode_stored_member", "_decode_deflated_member"):
        monkeypatch.setattr(_ooxml, name, observe_decoder(getattr(_ooxml, name)))

    # Repeat independent preparation calls on the same bytes: no cross-call
    # package cache may suppress either validation on a later invocation.
    for attempt in range(2):
        reads.clear()
        candidates.clear()
        validations.clear()
        decodes.clear()
        output = tmp_path / f"output-{attempt}.docx"
        result = (apply_edits(str(source), str(output), edits, preflight_proof=proof, producer_build=build)
                  if operation == "apply" else preflight_edits(str(source), edits, producer_build=build))
        assert result["status"] == "ok" and result["source_sha256"] == source_sha
        assert result["round_trip_check"]["status"] == "passed"
        assert len(reads) == len(candidates) == 1 and reads[0][0] == str(source)
        captured, candidate = reads[0][1], candidates[0]
        assert captured == before and candidate != before
        assert len(validations) == 2 and validations[0] is captured and validations[1] is candidate
        for payload in (captured, candidate):
            decoded = [(name, size, kept) for data, name, size, kept in decodes if data is payload]
            assert Counter(name for name, _, _ in decoded) == Counter({name: 1 for name in members})
            assert all(kept for _, _, kept in decoded)
            if payload is captured:
                assert sum(size for _, size, _ in decoded) == expanded
        assert all(data is captured or data is candidate for data, _, _, _ in decodes)
        candidate_sha = hashlib.sha256(candidate).hexdigest()
        if operation == "apply":
            assert result["preflight_binding_status"] == "verified"
            assert result["output_sha256"] == result["preflight_candidate_sha256"] == candidate_sha
            assert output.read_bytes() == candidate
        else:
            assert result["batch_applicable"] and result["preflight_proof"] == proof
            assert result["candidate_sha256"] == candidate_sha and not output.exists()
        assert source.read_bytes() == before


def test_encryption_is_rejected_consistently_on_every_surface(
    tmp_path: Path,
) -> None:
    encrypted_flag = 0x0001
    source = tmp_path / "encrypted.docx"
    source.write_bytes(
        _single_member_docx(flags=encrypted_flag, local_flags=encrypted_flag)
    )

    for surface in _SURFACES:
        _assert_surface_refusal(
            surface,
            source,
            tmp_path,
            expected_code="encrypted_docx",
            expected_list_reason="encrypted_docx",
        )


@pytest.mark.parametrize("fault", ["central", "opaque_crc", "opaque_budget", "opaque_collateral"])
def test_single_candidate_validation_retains_refusals_and_distinct_hashes(tmp_path, monkeypatch, fault):
    from veqtor_docx.synthetic import generate_demo_rounds
    from test_paragraph_edits import edit

    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("custom/opaque.bin", b"Preserved opaque bytes", compress_type=zipfile.ZIP_STORED)
    before = source.read_bytes()
    source_sha = hashlib.sha256(before).hexdigest()
    edits = [edit(source)]
    build = "candidate-validation-regression"
    proof = preflight_edits(str(source), edits, producer_build=build)["preflight_proof"]
    serialize = apply_module._output_archive_bytes
    candidates = []
    if fault == "opaque_budget":
        monkeypatch.setattr(_ooxml, "MAX_DOCX_OTHER_MEMBER_BYTES", 128)

    def corrupt(infos, parts):
        changed = dict(parts)
        if fault in {"opaque_collateral", "opaque_budget"}:
            changed["custom/opaque.bin"] = b"X" * (129 if fault == "opaque_budget" else 20)
        payload = serialize(infos, changed)
        if fault == "central":
            payload = payload[:-10]
        elif fault == "opaque_crc":
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                info = archive.getinfo("custom/opaque.bin")
            offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
            data = bytearray(payload)
            data[offset] ^= 1
            payload = bytes(data)
        candidates.append(payload)
        return payload

    monkeypatch.setattr(apply_module, "_output_archive_bytes", corrupt)
    code = {"central": "file_unextractable", "opaque_crc": "file_unextractable",
            "opaque_budget": "resource_limit_exceeded", "opaque_collateral": "round_trip_failed"}[fault]
    pre = preflight_edits(str(source), edits, producer_build=build)
    assert not pre["batch_applicable"] and pre["refusal_code"] == code
    assert pre["preflight_proof"] is None and pre["failure_phase"] == "round_trip"
    assert pre["source_sha256"] == source_sha
    assert pre["observed_candidate_sha256"] == hashlib.sha256(candidates[-1]).hexdigest()
    output = tmp_path / "never.docx"
    with pytest.raises(DocxError) as error:
        apply_edits(str(source), str(output), edits, preflight_proof=proof, producer_build=build)
    assert error.value.code == code
    assert error.value.metadata["observed_source_sha256"] == source_sha
    assert error.value.metadata["observed_candidate_sha256"] == hashlib.sha256(candidates[-1]).hexdigest()
    assert error.value.metadata["observed_candidate_sha256"] != source_sha
    assert error.value.metadata["failure_phase"] == "round_trip"
    assert error.value.metadata["round_trip_check"]["status"] == "failed"
    assert not output.exists() and not list(tmp_path.glob("*.veqtor-tmp"))
    assert source.read_bytes() == before


def test_inspection_package_consumer_reuses_retained_snapshot_without_io(tmp_path, monkeypatch):
    from veqtor_docx import inspect as inspect_module, inspect_document
    from veqtor_docx.synthetic import generate_demo_rounds

    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[1]
    payload = source.read_bytes()
    sha = hashlib.sha256(payload).hexdigest()
    expected = inspect_document(str(source), mode="browse", max_items=100)
    package = _ooxml.load_validated_docx(payload, capture=None)
    incomplete = _ooxml.load_validated_docx(payload, capture={"word/document.xml"})
    source.write_bytes(b"External replacement after the immutable capture")

    def no_io(*_args, **_kwargs):
        raise AssertionError("validated inspection consumer reread or revalidated the package")

    for module in (inspect_module, extract_module, apply_module, _ooxml):
        monkeypatch.setattr(module, "load_validated_docx", no_io)
        monkeypatch.setattr(module, "read_docx_payload", no_io)
    snapshot = inspect_module._snapshot_from_validated(package, path=str(source), file_sha256=sha)
    assert inspect_module._inspect_snapshot(snapshot, "browse", max_items=100) == expected
    with pytest.raises(inspect_module.InspectError) as error:
        inspect_module._snapshot_from_validated(incomplete, path=str(source), file_sha256=sha)
    assert error.value.code == "file_unextractable"
    assert error.value.metadata["observed_source_sha256"] == sha
