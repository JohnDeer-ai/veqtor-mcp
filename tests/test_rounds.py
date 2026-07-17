# SPDX-License-Identifier: Apache-2.0
"""list_rounds folder semantics: deterministic order, honest failure modes."""

import hashlib
import shutil
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from veqtor_docx import RoundError, extract_redlines, list_rounds
from veqtor_docx import _ooxml
from veqtor_docx import rounds as rounds_module


_DOCUMENT_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Round</w:t></w:r></w:p></w:body>
</w:document>
'''


def _write_round(
    path: Path,
    *,
    document: bytes = _DOCUMENT_XML,
    compression: int = zipfile.ZIP_DEFLATED,
    extra_members: tuple[tuple[str, bytes], ...] = (),
) -> None:
    with zipfile.ZipFile(path, "w", compression) as archive:
        archive.writestr("word/document.xml", document)
        for name, payload in extra_members:
            archive.writestr(name, payload)


def _with_bad_document_crc(payload: bytes) -> bytes:
    """Make local/central CRC agree with each other but not decoded bytes."""
    mutated = bytearray(payload)
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        document = archive.getinfo("word/document.xml")
    bad_crc = document.CRC ^ 1
    mutated[document.header_offset + 14 : document.header_offset + 18] = (
        bad_crc.to_bytes(4, "little")
    )

    cursor = 0
    while (cursor := payload.find(b"PK\x01\x02", cursor)) >= 0:
        name_length = int.from_bytes(payload[cursor + 28 : cursor + 30], "little")
        extra_length = int.from_bytes(payload[cursor + 30 : cursor + 32], "little")
        comment_length = int.from_bytes(payload[cursor + 32 : cursor + 34], "little")
        name = payload[cursor + 46 : cursor + 46 + name_length].decode("utf-8")
        if name == document.filename:
            mutated[cursor + 16 : cursor + 20] = bad_crc.to_bytes(4, "little")
            return bytes(mutated)
        cursor += 46 + name_length + extra_length + comment_length
    raise AssertionError("document central-directory record not found")


def _with_unsupported_document_compression(payload: bytes) -> bytes:
    """Patch both ZIP headers without making the package unparsable."""
    mutated = bytearray(payload)
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        document = archive.getinfo("word/document.xml")
    mutated[document.header_offset + 8 : document.header_offset + 10] = (99).to_bytes(
        2, "little"
    )

    cursor = 0
    central_signature = b"PK\x01\x02"
    while (cursor := payload.find(central_signature, cursor)) >= 0:
        name_length = int.from_bytes(payload[cursor + 28 : cursor + 30], "little")
        extra_length = int.from_bytes(payload[cursor + 30 : cursor + 32], "little")
        comment_length = int.from_bytes(payload[cursor + 32 : cursor + 34], "little")
        name = payload[cursor + 46 : cursor + 46 + name_length].decode("utf-8")
        if name == document.filename:
            mutated[cursor + 10 : cursor + 12] = (99).to_bytes(2, "little")
            return bytes(mutated)
        cursor += 46 + name_length + extra_length + comment_length
    raise AssertionError("document central-directory record not found")


def _with_invalid_utf8_member_name(payload: bytes) -> bytes:
    """Make one central/local member name invalid under its UTF-8 flag."""
    mutated = bytearray(payload)
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        member = archive.getinfo("word/document.xml")

    local = member.header_offset
    local_flags = int.from_bytes(payload[local + 6 : local + 8], "little") | 0x800
    mutated[local + 6 : local + 8] = local_flags.to_bytes(2, "little")
    local_name = local + 30
    mutated[local_name] = 0xFF

    cursor = 0
    while (cursor := payload.find(b"PK\x01\x02", cursor)) >= 0:
        name_length = int.from_bytes(payload[cursor + 28 : cursor + 30], "little")
        extra_length = int.from_bytes(payload[cursor + 30 : cursor + 32], "little")
        comment_length = int.from_bytes(payload[cursor + 32 : cursor + 34], "little")
        name = payload[cursor + 46 : cursor + 46 + name_length].decode("utf-8")
        if name == member.filename:
            central_flags = (
                int.from_bytes(payload[cursor + 8 : cursor + 10], "little") | 0x800
            )
            mutated[cursor + 8 : cursor + 10] = central_flags.to_bytes(2, "little")
            mutated[cursor + 46] = 0xFF
            return bytes(mutated)
        cursor += 46 + name_length + extra_length + comment_length
    raise AssertionError("document central-directory record not found")


def test_public_alpha_round_folder_envelope_is_frozen() -> None:
    assert rounds_module.MAX_ROUND_CANDIDATES == 500
    assert rounds_module.MAX_ROUND_TOTAL_INPUT_BYTES == 500 * 1024 * 1024
    assert rounds_module.MAX_ROUND_TOTAL_EXPANDED_BYTES == 500 * 1024 * 1024


def test_aggregate_round_expansion_limit_is_inclusive_and_resets_per_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_round(tmp_path / "round-1.docx")
    _write_round(tmp_path / "round-2.docx")
    exact_total = 2 * len(_DOCUMENT_XML)
    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        exact_total,
    )

    first = list_rounds(str(tmp_path))
    second = list_rounds(str(tmp_path))

    assert len(first["rounds"]) == 2
    assert len(second["rounds"]) == 2

    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        exact_total - 1,
    )
    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path))

    assert error.value.code == "resource_limit_exceeded"
    assert "aggregate expanded-output limit" in str(error.value)
    assert "split the folder and retry" in str(error.value)


def test_deflate_budget_refuses_at_the_first_byte_over_limit(tmp_path: Path) -> None:
    source = tmp_path / "round.docx"
    _write_round(source)
    budget = _ooxml.ExpandedOutputBudget(
        allowed_bytes=len(_DOCUMENT_XML) - 1,
        limit="test_expanded_bytes",
    )

    with pytest.raises(_ooxml.ExpandedOutputBudgetExceeded) as error:
        _ooxml.load_validated_docx(
            source.read_bytes(),
            capture=(_ooxml.DOCUMENT_PART,),
            expanded_budget=budget,
        )

    assert budget.consumed_bytes == len(_DOCUMENT_XML)
    assert budget.remaining_bytes == 0
    assert error.value.metadata == {
        "limit": "test_expanded_bytes",
        "allowed_bytes": len(_DOCUMENT_XML) - 1,
        "observed_bytes": len(_DOCUMENT_XML),
        "observed_at_least": True,
    }


@pytest.mark.parametrize(
    "compression",
    [zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED],
    ids=["deflated", "stored"],
)
def test_late_crc_skip_still_consumes_the_round_scan_budget(
    compression: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = tmp_path / "00-bad-crc.docx"
    good = tmp_path / "01-good.docx"
    _write_round(bad, compression=compression)
    bad.write_bytes(_with_bad_document_crc(bad.read_bytes()))
    _write_round(good)
    exact_total = 2 * len(_DOCUMENT_XML)

    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        exact_total,
    )
    within_limit = list_rounds(str(tmp_path))
    assert [item["filename"] for item in within_limit["rounds"]] == [good.name]
    assert within_limit["skipped"] == [
        {"filename": bad.name, "reason": "invalid_docx"}
    ]

    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        exact_total - 1,
    )
    with pytest.raises(RoundError, match="aggregate expanded-output limit"):
        list_rounds(str(tmp_path))


def test_multiple_late_refusals_accumulate_before_the_next_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_crc = tmp_path / "00-bad-crc.docx"
    malformed = tmp_path / "01-malformed.docx"
    good = tmp_path / "02-good.docx"
    _write_round(bad_crc)
    bad_crc.write_bytes(_with_bad_document_crc(bad_crc.read_bytes()))
    malformed_output = b"<malformed"
    _write_round(malformed, document=malformed_output)
    _write_round(good)
    exact_total = 2 * len(_DOCUMENT_XML) + len(malformed_output)

    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        exact_total,
    )
    within_limit = list_rounds(str(tmp_path))
    assert [item["filename"] for item in within_limit["rounds"]] == [good.name]
    assert within_limit["skipped"] == [
        {"filename": bad_crc.name, "reason": "invalid_docx"},
        {"filename": malformed.name, "reason": "malformed_xml"},
    ]

    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        exact_total - 1,
    )
    with pytest.raises(RoundError, match="aggregate expanded-output limit"):
        list_rounds(str(tmp_path))


@pytest.mark.parametrize("late_refusal", ["malformed_xml", "missing_part"])
def test_late_round_refusals_are_charged_before_the_next_file(
    late_refusal: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = tmp_path / "00-refused.docx"
    good = tmp_path / "01-good.docx"
    if late_refusal == "malformed_xml":
        refused_output = b"<malformed"
        _write_round(refused, document=refused_output)
    else:
        refused_output = b"missing document output"
        with zipfile.ZipFile(refused, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("custom/data.bin", refused_output)
    _write_round(good)
    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        len(refused_output) + len(_DOCUMENT_XML) - 1,
    )

    with pytest.raises(RoundError, match="aggregate expanded-output limit"):
        list_rounds(str(tmp_path))


def test_predecode_skip_leaves_the_full_round_scan_budget_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported = tmp_path / "00-unsupported.docx"
    good = tmp_path / "01-good.docx"
    _write_round(unsupported)
    unsupported.write_bytes(
        _with_unsupported_document_compression(unsupported.read_bytes())
    )
    _write_round(good)
    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        len(_DOCUMENT_XML),
    )

    result = list_rounds(str(tmp_path))

    assert [item["filename"] for item in result["rounds"]] == [good.name]
    assert result["skipped"] == [
        {"filename": unsupported.name, "reason": "unsupported_compression"}
    ]


def test_round_scan_stops_in_filename_order_after_budget_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(3):
        _write_round(tmp_path / f"round-{index}.docx")
    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        len(_DOCUMENT_XML) + 1,
    )
    observed: list[str] = []
    original_read = rounds_module.read_docx_payload
    original_iterdir = Path.iterdir

    def shuffled_iterdir(path: Path):
        if path == tmp_path:
            return iter(
                [
                    tmp_path / "round-2.docx",
                    tmp_path / "round-0.docx",
                    tmp_path / "round-1.docx",
                ]
            )
        return original_iterdir(path)

    def observe_read(path: Path) -> bytes:
        observed.append(path.name)
        return original_read(path)

    monkeypatch.setattr(Path, "iterdir", shuffled_iterdir)
    monkeypatch.setattr(rounds_module, "read_docx_payload", observe_read)
    with pytest.raises(RoundError, match="aggregate expanded-output limit"):
        list_rounds(str(tmp_path))

    assert observed == ["round-0.docx", "round-1.docx"]


def test_filename_order_has_a_deterministic_casefold_tiebreak() -> None:
    paths = [Path(name) for name in ("a.docx", "B.docx", "A.docx", "b.docx")]

    assert [
        path.name for path in sorted(paths, key=rounds_module._round_filename_key)
    ] == [
        "A.docx",
        "a.docx",
        "B.docx",
        "b.docx",
    ]


def test_numbered_round_names_keep_v1_lexicographic_order_and_disclose_basis(
    tmp_path: Path,
) -> None:
    for filename in ("Round 1.docx", "Round 10.docx", "Round 2.docx"):
        _write_round(tmp_path / filename)

    result = list_rounds(str(tmp_path))

    assert [item["filename"] for item in result["rounds"]] == [
        "Round 1.docx",
        "Round 10.docx",
        "Round 2.docx",
    ]
    assert result["ordering_source"] == "filename_lexicographic_v1"
    assert result["order_basis"] == {
        "kind": "filename",
        "rule": "casefold_then_exact",
        "lineage_verified": False,
        "round_id_semantics": "position_only",
    }


def test_explicit_filename_sequence_controls_position_without_claiming_lineage(
    tmp_path: Path,
) -> None:
    for filename in ("Round 1.docx", "Round 10.docx", "Round 2.docx"):
        _write_round(tmp_path / filename)

    result = list_rounds(
        str(tmp_path),
        ordered_filenames=["Round 1.docx", "Round 2.docx", "Round 10.docx"],
    )

    assert [item["filename"] for item in result["rounds"]] == [
        "Round 1.docx",
        "Round 2.docx",
        "Round 10.docx",
    ]
    assert [item["round_id"] for item in result["rounds"]] == [
        "round-001",
        "round-002",
        "round-003",
    ]
    assert result["ordering_source"] == "explicit_filename_sequence_v1"
    assert result["order_basis"] == {
        "kind": "caller_supplied_filename_sequence",
        "lineage_verified": False,
        "round_id_semantics": "position_only",
    }


@pytest.mark.parametrize(
    "ordered_filenames",
    [
        ["Round 1.docx", "Round 2.docx"],
        ["Round 1.docx", "Round 2.docx", "missing.docx"],
        ["Round 1.docx", "Round 1.docx", "Round 2.docx"],
        ["Round 1.docx", "../Round 2.docx", "Round 10.docx"],
    ],
    ids=["missing-entry", "unknown-entry", "duplicate", "not-a-basename"],
)
def test_explicit_filename_sequence_fails_closed_on_invalid_manifest(
    ordered_filenames: list[str],
    tmp_path: Path,
) -> None:
    for filename in ("Round 1.docx", "Round 10.docx", "Round 2.docx"):
        _write_round(tmp_path / filename)

    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path), ordered_filenames=ordered_filenames)

    assert error.value.code == "invalid_round_order"


def test_lists_rounds_in_filename_order(demo_dir: Path) -> None:
    result = list_rounds(str(demo_dir))
    assert [r["round_id"] for r in result["rounds"]] == [
        "round-001",
        "round-002",
        "round-003",
        "round-004",
    ]
    filenames = [r["filename"] for r in result["rounds"]]
    assert filenames == sorted(filenames)
    assert result["skipped"] == []


def test_hashes_and_revision_counts_match_extraction(demo_dir: Path) -> None:
    for entry in list_rounds(str(demo_dir))["rounds"]:
        payload = Path(entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["revision_count"] == extract_redlines(entry["path"])["revision_count"]


def test_ignores_junk_and_survives_corrupt_files(demo_dir: Path, tmp_path: Path) -> None:
    folder = tmp_path / "deal"
    shutil.copytree(demo_dir, folder)
    (folder / "~$round-2-counterparty-redline.docx").write_bytes(b"\x00\x01lock")
    (folder / "notes.txt").write_text("not a round")
    (folder / "00-broken.docx").write_bytes(b"this is not a zip archive")
    (folder / "sub").mkdir()
    shutil.copy(next(demo_dir.glob("*.docx")), folder / "sub" / "nested.docx")

    result = list_rounds(str(folder))
    assert [r["filename"] for r in result["rounds"]] == [
        "round-1-outgoing-draft.docx",
        "round-2-counterparty-redline.docx",
        "round-3-our-counter.docx",
        "round-4-counterparty-reply.docx",
    ]
    # Valid rounds keep contiguous ids even when a broken file sorts first.
    assert [r["round_id"] for r in result["rounds"]] == [
        "round-001",
        "round-002",
        "round-003",
        "round-004",
    ]
    assert [s["filename"] for s in result["skipped"]] == ["00-broken.docx"]
    assert result["skipped"][0]["reason"] == "invalid_docx"


def test_missing_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path / "nope"))
    assert error.value.code == "not_a_folder"


def test_unsupported_zip_compression_is_a_stable_skip(
    demo_dir: Path, tmp_path: Path
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    broken = tmp_path / "unsupported-compression.docx"
    broken.write_bytes(_with_unsupported_document_compression(source.read_bytes()))

    result = list_rounds(str(tmp_path))

    assert result["rounds"] == []
    assert result["skipped"] == [
        {
            "filename": broken.name,
            "reason": "unsupported_compression",
        }
    ]
    with pytest.raises(rounds_module.DocxError):
        extract_redlines(str(broken))


def test_invalid_utf8_zip_member_name_is_a_stable_skip(
    demo_dir: Path, tmp_path: Path
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    broken = tmp_path / "invalid-utf8-name.docx"
    broken.write_bytes(_with_invalid_utf8_member_name(source.read_bytes()))

    result = list_rounds(str(tmp_path))

    assert result["rounds"] == []
    assert result["skipped"] == [
        {"filename": broken.name, "reason": "invalid_docx"}
    ]


def test_unreadable_folder_is_a_stable_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_iterdir = Path.iterdir

    def deny(target: Path):
        if target == tmp_path:
            raise PermissionError("private filesystem detail")
        return original_iterdir(target)

    monkeypatch.setattr(Path, "iterdir", deny)
    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path))
    assert error.value.code == "folder_unreadable"
    assert str(error.value) == "folder_unreadable: cannot enumerate folder"


def test_candidate_count_is_bounded_before_any_docx_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(3):
        (tmp_path / f"round-{index}.docx").write_bytes(b"placeholder")
    monkeypatch.setattr(rounds_module, "MAX_ROUND_CANDIDATES", 2)

    def fail_read(_path: Path, *, expanded_budget):
        raise AssertionError("candidate was read before folder preflight")

    monkeypatch.setattr(rounds_module, "_round_facts", fail_read)

    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path))

    assert error.value.code == "resource_limit_exceeded"
    assert str(error.value) == (
        "resource_limit_exceeded: folder contains more than 2 candidate DOCX files"
    )


def test_aggregate_round_input_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(2):
        (tmp_path / f"round-{index}.docx").write_bytes(b"invaliddoc")
    monkeypatch.setattr(rounds_module, "MAX_ROUND_TOTAL_INPUT_BYTES", 19)

    def fail_read(_path: Path, *, expanded_budget):
        raise AssertionError("candidate was read before aggregate preflight")

    monkeypatch.setattr(rounds_module, "_round_facts", fail_read)

    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path))

    assert error.value.code == "resource_limit_exceeded"
    assert "aggregate input limit" in str(error.value)


def test_unexpected_file_failure_is_not_leaked_as_a_successful_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "matter"
    folder.mkdir()
    (folder / "round.docx").write_bytes(b"placeholder")
    sentinel = "private client sentinel"

    def explode(_path: Path, *, expanded_budget):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(rounds_module, "_round_facts", explode)
    with pytest.raises(RuntimeError, match=sentinel):
        list_rounds(str(folder))


def test_user_home_folder_is_expanded(demo_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(demo_dir.parent))
    result = list_rounds(f"~/{demo_dir.name}")
    assert len(result["rounds"]) == 4
    assert "~" not in result["folder"]
    assert all("~" not in r["path"] for r in result["rounds"])
