# SPDX-License-Identifier: Apache-2.0
"""Enumerate DOCX negotiation rounds in a folder."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from ._ooxml import DOCUMENT_PART, TEXT_REVISION_TAGS, parse_xml


def _revision_count(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        document = parse_xml(zf.read(DOCUMENT_PART))
    return sum(1 for el in document.iter() if el.tag in TEXT_REVISION_TAGS)


def list_rounds(folder: str) -> dict:
    """List DOCX rounds in ``folder``, sorted by filename.

    Filename order is the deterministic v1 round order; Word lock files
    (``~$*``) are ignored and the scan is non-recursive. Files that cannot be
    read as DOCX end up in ``skipped`` instead of failing the whole call.
    """
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {folder}")

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

    rounds: list[dict] = []
    skipped: list[dict] = []
    for path in candidates:
        try:
            revision_count = _revision_count(path)
        except Exception as exc:  # corrupt, encrypted or non-OOXML file
            skipped.append({"filename": path.name, "reason": str(exc)})
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
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
