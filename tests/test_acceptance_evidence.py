# SPDX-License-Identifier: Apache-2.0
"""The external I8 evidence packet is exact-SHA bound and privacy-shaped."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_acceptance_evidence import (  # noqa: E402
    EvidenceError,
    SCHEMA_VERSION,
    _load_packet,
    validate_evidence,
)
from release_contract import FIVE_EDIT_OUTPUT_SHA256  # noqa: E402


CANDIDATE_SHA = "a" * 40
CANDIDATE_TREE = "b" * 40
PRODUCER_BUILD = "source-snapshot-v1-sha256:" + "c" * 64
CORPUS_SHA = "d" * 64
OUTPUT_SHA = FIVE_EDIT_OUTPUT_SHA256


def _packet() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "producer_build": PRODUCER_BUILD,
        "public_matrix": {
            "python_3_12": "passed",
            "python_3_13": "passed",
            "python_3_14": "passed",
            "minimum_direct": "passed",
        },
        "private_dogfood": {
            "used": {
                "passed": 4,
                "skipped": 1,
                "corpus_before_sha256": CORPUS_SHA,
                "corpus_after_sha256": CORPUS_SHA,
            },
            "clean": {
                "passed": 4,
                "skipped": 1,
                "corpus_before_sha256": CORPUS_SHA,
                "corpus_after_sha256": CORPUS_SHA,
            },
        },
        "payment_preflight": {
            "batch_applicable": False,
            "refusal_code": "counter_position_unsupported",
            "match_count": 1,
        },
        "five_edit_batch": {
            "preflight_applicable": True,
            "apply_status": "ok",
            "applied_count": 5,
            "round_trip_status": "passed",
            "collateral_change_count": 0,
            "output_sha256": OUTPUT_SHA,
        },
    }


def _validate(packet: dict) -> None:
    validate_evidence(
        packet,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        producer_build=PRODUCER_BUILD,
    )


def test_complete_exact_candidate_evidence_passes() -> None:
    _validate(_packet())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda packet: packet.update({"private_path": "/Users/example/matter"}),
            "fields differ",
        ),
        (
            lambda packet: packet.update({"candidate_sha": "f" * 40}),
            "candidate does not equal",
        ),
        (
            lambda packet: packet["private_dogfood"]["used"].update(
                {"corpus_after_sha256": "f" * 64}
            ),
            "modified the source corpus",
        ),
        (
            lambda packet: packet["payment_preflight"].update(
                {"match_count": 0}
            ),
            "expected refusal",
        ),
        (
            lambda packet: packet["five_edit_batch"].update(
                {"collateral_change_count": 1}
            ),
            "collateral changes",
        ),
        (
            lambda packet: packet["five_edit_batch"].update(
                {"output_sha256": "0" * 64}
            ),
            "output fingerprint differs",
        ),
    ],
    ids=[
        "extra_private_field",
        "wrong_sha",
        "corpus_changed",
        "payment",
        "collateral",
        "wrong_output_sha",
    ],
)
def test_incomplete_or_non_private_shape_fails(mutate, message: str) -> None:
    packet = copy.deepcopy(_packet())
    mutate(packet)
    with pytest.raises(EvidenceError, match=message):
        _validate(packet)


def test_evidence_loader_is_bounded_and_rejects_duplicate_keys(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"one","schema_version":"two"}')
    with pytest.raises(EvidenceError, match="duplicate keys"):
        _load_packet(duplicate)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(json.dumps(_packet()).encode() + b" " * (64 * 1024))
    with pytest.raises(EvidenceError, match="size limit"):
        _load_packet(oversized)
