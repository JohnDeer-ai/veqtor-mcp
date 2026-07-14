# SPDX-License-Identifier: Apache-2.0
"""verify_quote: deterministic quote checking against read-path anchors."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
import veqtor_docx.extract as extract_module

from veqtor_docx import VerifyError, extract_redlines, verify_quote
from veqtor_docx.synthetic import CAP_R2


@pytest.fixture
def cap(demo_dir: Path) -> tuple[str, dict]:
    path = str(demo_dir / "round-2-counterparty-redline.docx")
    result = extract_redlines(path)
    unit = next(
        u
        for u in result["change_units"]
        if u["clause_anchor"] and u["clause_anchor"]["label"] == "14.2"
    )
    return path, {
        "change_unit_id": unit["change_unit_id"],
        "file_sha256": result["file_sha256"],
    }


def test_exact_quote_from_new_text(cap) -> None:
    path, anchor = cap
    result = verify_quote(path, anchor, CAP_R2)
    assert result["verdict"] == "exact"
    assert result["exact"] is True
    assert result["diff"] == []
    (match,) = result["matches"]
    assert match["side"] == "new"
    assert match["clause"] == "14.2 Limitation of Liability"
    assert match["part_name"] == "word/document.xml"
    assert match["revision_ids"]


def test_exact_substring_from_old_text(cap) -> None:
    path, anchor = cap
    result = verify_quote(path, anchor, "twelve (12) months preceding")
    assert result["verdict"] == "exact"
    assert result["matches"][0]["side"] == "old"


def test_normalized_quote_collapsed_whitespace(cap) -> None:
    path, anchor = cap
    result = verify_quote(path, anchor, "the total fees paid by Client  under this Agreement")
    assert result["verdict"] == "normalized"
    assert result["exact"] is False
    assert result["matches"][0]["side"] == "old"
    assert result["diff"]


def test_typographic_drift_normalizes(cap) -> None:
    path, anchor = cap
    result = verify_quote(path, anchor, "USD 50,000")  # non-breaking space
    assert result["verdict"] == "normalized"


def test_not_found(cap) -> None:
    path, anchor = cap
    result = verify_quote(path, anchor, "USD 999,999")
    assert result["verdict"] == "not_found"
    assert result["exact"] is False
    assert result["matches"] == []
    assert result["diff"]


def test_case_sensitive(cap) -> None:
    path, anchor = cap
    assert verify_quote(path, anchor, "usd 50,000")["verdict"] == "not_found"


@pytest.mark.parametrize(
    "blank",
    ["", " ", "  ", "\t\n ", " ", "  ", 42, None],
)
def test_blank_or_nonstring_quotes_are_refused(cap, blank) -> None:
    """A whitespace-only quote is a substring of almost any text; verifying
    it as exact would be a false-positive surface for a trust tool."""
    path, anchor = cap
    with pytest.raises(VerifyError) as err:
        verify_quote(path, anchor, blank)
    assert err.value.code == "quote_missing"


def test_missing_file_is_a_stable_error(cap, tmp_path: Path) -> None:
    _, anchor = cap
    with pytest.raises(VerifyError) as err:
        verify_quote(str(tmp_path / "nope.docx"), anchor, "USD 50,000")
    assert err.value.code == "file_unreadable"
    assert err.value.metadata == {}


def test_malformed_document_xml_is_a_stable_error(demo_dir: Path, tmp_path: Path) -> None:
    """A real zip with a truncated word/document.xml must not leak a raw
    lxml XMLSyntaxError past the sha check — same file_unextractable code."""
    from veqtor_docx import DocxError, extract_redlines as extract

    source = zipfile.ZipFile(demo_dir / "round-1-outgoing-draft.docx")
    broken = tmp_path / "truncated.docx"
    with zipfile.ZipFile(broken, "w") as zf:
        for name in source.namelist():
            payload = source.read(name)
            if name == "word/document.xml":
                payload = payload[: len(payload) // 2]  # cut mid-element
            zf.writestr(name, payload)

    with pytest.raises(DocxError):
        extract(str(broken))  # the whole read path shares the boundary

    real_sha = hashlib.sha256(broken.read_bytes()).hexdigest()
    with pytest.raises(VerifyError) as err:
        verify_quote(
            str(broken),
            {"change_unit_id": "cu_001", "file_sha256": real_sha},
            "anything",
        )
    assert err.value.code == "file_unextractable"
    assert err.value.metadata == {
        "claimed_source_sha256": real_sha,
        "observed_source_sha256": real_sha,
    }


def test_readable_but_unextractable_docx_is_a_stable_error(tmp_path: Path) -> None:
    """A zip that opens fine but is not a Word package must not leak a bare
    DocxError past the sha check."""
    bogus = tmp_path / "bogus.docx"
    with zipfile.ZipFile(bogus, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    real_sha = hashlib.sha256(bogus.read_bytes()).hexdigest()
    with pytest.raises(VerifyError) as err:
        verify_quote(
            str(bogus),
            {"change_unit_id": "cu_001", "file_sha256": real_sha},
            "anything",
        )
    assert err.value.code == "file_unextractable"
    assert err.value.metadata == {
        "claimed_source_sha256": real_sha,
        "observed_source_sha256": real_sha,
    }


def test_fail_closed_errors(cap, demo_dir: Path) -> None:
    path, anchor = cap
    with pytest.raises(VerifyError) as err:
        verify_quote(path, anchor, "")
    assert err.value.code == "quote_missing"

    with pytest.raises(VerifyError) as err:
        verify_quote(path, {**anchor, "file_sha256": "0" * 64}, CAP_R2)
    assert err.value.code == "file_sha256_mismatch"
    assert err.value.metadata == {
        "claimed_source_sha256": "0" * 64,
        "observed_source_sha256": anchor["file_sha256"],
    }

    with pytest.raises(VerifyError) as err:
        verify_quote(path, {**anchor, "change_unit_id": "cu_999"}, CAP_R2)
    assert err.value.code == "anchor_not_found"
    assert err.value.metadata == {
        "claimed_source_sha256": anchor["file_sha256"],
        "observed_source_sha256": anchor["file_sha256"],
    }

    with pytest.raises(VerifyError) as err:
        verify_quote(path, {"change_unit_id": anchor["change_unit_id"]}, CAP_R2)
    assert err.value.code == "anchor_missing"
    assert err.value.metadata == {}


def test_verdict_and_hash_come_from_one_snapshot(demo_dir: Path, monkeypatch) -> None:
    """TOCTOU regression: if the file is swapped while verify_quote runs, the
    verdict and checked_anchor must still describe ONE snapshot. The tool
    reads the file exactly once, so a swap after that read cannot split the
    provenance."""
    r2 = demo_dir / "round-2-counterparty-redline.docx"
    r4 = demo_dir / "round-4-counterparty-reply.docx"
    payload_r2, payload_r4 = r2.read_bytes(), r4.read_bytes()
    sha_r2 = hashlib.sha256(payload_r2).hexdigest()
    extraction = extract_redlines(str(r2))
    cap_unit = next(
        u
        for u in extraction["change_units"]
        if u["clause_anchor"] and u["clause_anchor"]["label"] == "14.2"
    )

    reads = {"count": 0}
    original_read_payload = extract_module.read_docx_payload

    def swapping_read_payload(path: str) -> bytes:
        if Path(path) == r2:
            reads["count"] += 1
            # First read sees round 2; any later read would see round 4.
            return payload_r2 if reads["count"] == 1 else payload_r4
        return original_read_payload(path)

    monkeypatch.setattr(extract_module, "read_docx_payload", swapping_read_payload)
    result = verify_quote(
        str(r2),
        {"change_unit_id": cap_unit["change_unit_id"], "file_sha256": sha_r2},
        CAP_R2,
    )
    assert reads["count"] == 1, "verification must consume exactly one snapshot"
    assert result["verdict"] == "exact"
    assert result["checked_anchor"]["file_sha256"] == sha_r2


def test_verify_is_deterministic(cap) -> None:
    path, anchor = cap
    first = verify_quote(path, anchor, CAP_R2)
    second = verify_quote(path, anchor, CAP_R2)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
