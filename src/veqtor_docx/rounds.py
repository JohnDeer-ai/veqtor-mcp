# SPDX-License-Identifier: Apache-2.0
"""Enumerate DOCX negotiation rounds in a folder."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from ._ooxml import (
    DOCUMENT_PART,
    DocxError,
    TEXT_REVISION_TAGS,
    UserPathError,
    ZIP_READ_ERRORS,
    parse_xml,
    resolve_user_path,
)

_SUPPORTED_DOCX_COMPRESSION = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
)


class RoundError(DocxError):
    """A controlled list-rounds refusal or per-file skip reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _round_facts(path: Path) -> tuple[str, int]:
    """sha256 and revision count from one byte snapshot of the file."""
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RoundError("file_unreadable", "cannot read DOCX bytes") from exc
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except ZIP_READ_ERRORS as exc:
        raise RoundError("invalid_docx", "invalid DOCX package") from exc
    with archive:
        try:
            document_info = archive.getinfo(DOCUMENT_PART)
        except KeyError as exc:
            raise RoundError(
                "missing_document_part", "DOCX has no main document part"
            ) from exc
        if document_info.flag_bits & 0x1:
            raise RoundError("encrypted_docx", "main document part is encrypted")
        if document_info.compress_type not in _SUPPORTED_DOCX_COMPRESSION:
            raise RoundError(
                "unsupported_compression",
                "main document part uses unsupported ZIP compression",
            )
        try:
            document_payload = archive.read(document_info)
        except ZIP_READ_ERRORS as exc:
            raise RoundError("invalid_docx", "cannot read main document part") from exc
    try:
        document = parse_xml(document_payload)
    except DocxError as exc:
        raise RoundError("malformed_xml", "main document XML is malformed") from exc
    count = sum(1 for el in document.iter() if el.tag in TEXT_REVISION_TAGS)
    return hashlib.sha256(payload).hexdigest(), count


def list_rounds(folder: str) -> dict:
    """List DOCX rounds in ``folder``, sorted by filename.

    Filename order is the deterministic v1 round order; Word lock files
    (``~$*``) are ignored and the scan is non-recursive. Files that cannot be
    read as DOCX end up in ``skipped`` instead of failing the whole call.
    """
    try:
        root = Path(resolve_user_path(folder))
    except UserPathError as exc:
        raise RoundError(exc.code, exc.detail) from exc
    if not root.is_dir():
        raise RoundError("not_a_folder", "folder is not a directory")

    try:
        candidates = sorted(
            (
                p
                for p in root.iterdir()
                if p.is_file()
                and p.suffix.lower() == ".docx"
                and not p.name.startswith("~$")
            ),
            key=lambda p: p.name.casefold(),
        )
    except OSError as exc:
        raise RoundError("folder_unreadable", "cannot enumerate folder") from exc

    rounds: list[dict] = []
    skipped: list[dict] = []
    for path in candidates:
        try:
            digest, revision_count = _round_facts(path)
        except RoundError as exc:
            skipped.append({"filename": path.name, "reason": exc.code})
            continue
        rounds.append(
            {
                "round_id": f"round-{len(rounds) + 1:03d}",
                "path": str(path),
                "filename": path.name,
                "sha256": digest,
                "revision_count": revision_count,
            }
        )
    return {"folder": str(root), "rounds": rounds, "skipped": skipped}
