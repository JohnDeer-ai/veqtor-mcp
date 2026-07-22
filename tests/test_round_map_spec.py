# SPDX-License-Identifier: Apache-2.0
"""Ratchets for the frozen Round Map contract and implemented registry."""

import json
import re
import tomllib
from pathlib import Path

from veqtor_mcp import records


ROOT = Path(__file__).parents[1]
SPEC_PATH = ROOT / "ROUND_MAP_V0.3.md"
V012_FIXTURE_PATH = ROOT / "tests/data/round-map-v0.1.2-preflightless-apply-record.json"


def _spec() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


def _v012_fixture() -> dict:
    return json.loads(V012_FIXTURE_PATH.read_text(encoding="utf-8"))


def test_round_map_spec_is_packaged_and_runtime_registers_the_permanent_pair() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sdist_includes = project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    assert "/ROUND_MAP_V0.3.md" in sdist_includes
    assert len(records.WRITABLE_TOOL_NAMES) == 8
    assert "map_rounds" in records.WRITABLE_TOOL_NAMES
    assert records.V1_HISTORICAL_TOOL_SPECS["map_rounds"].record_type == (
        "round_map.v1"
    )

    spec = _spec()
    assert "a design and acceptance specification, not an implementation claim" in spec
    assert "`map_rounds`" in spec
    assert "`round_map.v1`" in spec
    assert "continues to expose exactly seven tools" in spec
    assert "reuses the exact metadata tuple already\nenforced" in spec
    assert "future emission invariant" not in spec


def test_round_map_spec_keeps_claim_classes_and_uncertainty_separate() -> None:
    spec = _spec()

    for relationship_type in (
        "`recorded_derivation`",
        "`exact_content_equality`",
        "`navigation_candidate`",
    ):
        assert relationship_type in spec
    for resolution_state in ("`exact_unique`", "`ambiguous`", "`unresolved`"):
        assert resolution_state in spec

    assert "The journal is mutable" in spec
    assert re.search(r"directed\s+document-level relationship", spec)
    assert "complete `accepted_current_v1` text" in spec
    assert "never be collapsed into a confidence score" in spec
    assert "`unresolved` never means deleted" in spec
    assert re.search(r"`chronology_verified` are `false` for every type", spec)
    assert "`derivation_recorded: true`" in spec
    assert "Generic `lineage_verified`" in spec


def test_round_map_spec_closes_snapshot_identity_and_graph_boundaries() -> None:
    spec = _spec()

    for required in (
        "`cross_source_atomic` is therefore always `false`",
        '`document_id = "rm_doc_v1:" + file_sha256`',
        "A rename creates a new\nobservation identity",
        "recorded connected component, traversed in both directions",
        "Cycles and self-loops are preserved",
        "`rm1` cursor",
        "complete ordered array of closed items before pagination",
        "journal bytes | 64 MiB",
        "never contains paragraph/contract body text",
    ):
        assert required in spec

    assert re.search(r"absent-journal to\s+own-record-only creation", spec)
    assert "Exclude the seed's identical\n   `paragraph_id` itself" in spec
    assert '"total_map_items": 70000' in spec
    assert "`frozen_legacy_v1`" in spec
    assert "`published_v0.1.2_preflightless`" in spec
    assert "`VEQTOR_DISABLE_DECISION_RECORD`" in spec
    assert re.search(
        r"Compact export contains no paths, filenames, labels, headings, snippets",
        spec,
    )


def test_round_map_spec_has_closed_failure_and_acceptance_matrices() -> None:
    spec = _spec()

    for error_code in (
        "`workspace_changed`",
        "`cursor_mismatch`",
        "`journal_busy`",
        "`journal_corrupt`",
        "`journal_oversize`",
        "`resource_limit_exceeded`",
        "`output_contract_error`",
    ):
        assert error_code in spec

    acceptance = spec.split("## Acceptance fixtures for later implementation", 1)[1]
    fixture_numbers = [
        int(match.group(1))
        for match in re.finditer(r"(?m)^(\d+)\. ", acceptance.split("\n## ", 1)[0])
    ]
    assert fixture_numbers == list(range(1, 28))
    assert "installed-wheel and package-reproducibility workflows" in acceptance
    assert re.search(
        r"does\s+not upgrade the local journal to tamper-evident provenance", spec
    )


def test_round_map_json_examples_are_well_formed() -> None:
    blocks = re.findall(r"```json\n(.*?)\n```", _spec(), re.DOTALL)

    assert blocks
    for block in blocks:
        assert isinstance(json.loads(block), dict)


def test_round_map_spec_accepts_only_the_exact_published_v012_profile() -> None:
    fixture = _v012_fixture()
    records._validated_record_bytes(fixture)

    result = fixture["result"]
    provenance = fixture["provenance"]
    assert fixture["producer"]["version"] == "0.1.2"
    assert result["source_sha256"] == provenance["source_sha256"]
    assert result["output_sha256"] == provenance["output_sha256"]
    assert (
        result["round_trip_check"]
        == provenance["round_trip_check"]
        == {
            "status": "passed",
            "collateral_changes": [],
            "comparison": "ooxml_semantic_diff_outside_touched_anchors",
        }
    )
    strengthened = {
        "preflight_binding_status",
        "preflight_candidate_sha256",
        "candidate_output_sha256_match",
    }
    assert strengthened.isdisjoint(result)
    assert strengthened.isdisjoint(provenance)

    spec = _spec()
    assert str(V012_FIXTURE_PATH.relative_to(ROOT)) in spec
    assert "`published_v0_1_2_only`" in spec
    assert "If all six are absent" in spec
    assert "Any other presence pattern is\n   `unsupported_legacy_profile`" in spec


def test_round_map_spec_closes_conflict_endpoints_and_digests() -> None:
    spec = _spec()
    endpoint_section = spec.split("The exact per-reason extraction table is:", 1)[1]
    endpoint_section = endpoint_section.split(
        "Valid branching, multiple parents, cycles", 1
    )[0]
    for reason in (
        "result_status_invalid",
        "missing_source_sha256",
        "invalid_source_sha256",
        "missing_output_sha256",
        "invalid_output_sha256",
        "result_output_sha256_mismatch",
        "round_trip_missing",
        "round_trip_failed",
        "round_trip_comparison_unsupported",
        "round_trip_fact_mismatch",
        "result_source_sha256_mismatch",
        "preflight_binding_status_invalid",
        "preflight_candidate_sha256_mismatch",
        "candidate_output_sha256_match_invalid",
        "strengthened_fact_mismatch",
        "unsupported_legacy_profile",
    ):
        assert f"`{reason}`" in endpoint_section

    fixtures = [
        json.loads(block)
        for block in re.findall(r"```json\n(.*?)\n```", spec, re.DOTALL)
    ]
    fixture = next(
        item
        for item in fixtures
        if item.get("schema_version") == "round_map_conflict_endpoint_fixture.v1"
    )
    expected = fixture["expected"]
    current_id = "rm_doc_v1:" + fixture["current_document_sha256"]
    assert expected["reason"] == "result_output_sha256_mismatch"
    assert expected["affected_document_ids"] == [current_id]
    assert expected["resolution_state"] == "ambiguous"
    assert expected["resolution_reason"] == "recorded_fact_conflict"
    assert expected["conflict_count"] == 1
    assert expected["candidate_id_count"] == 0
    assert expected["journal_snapshot_includes_record"] is True
    assert expected["full_result_set_includes_conflict_and_resolution"] is True


def test_current_docs_distinguish_frozen_acceptance_from_eight_tool_runtime() -> None:
    api = (ROOT / "API.md").read_text(encoding="utf-8")
    limitations = (ROOT / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")
    backlog = (ROOT / "POST-V0.1-BACKLOG.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "preimplementation acceptance contract" in api
    assert "permanent eighth tool" in api
    assert "seven tools other than `map_rounds`" in api
    assert "success-only `round_map.v1` record" in api
    assert "Pre-result Map refusals do not append" in limitations
    assert "eighth tool and\npermanent success-only record pair" in backlog
    assert "`map_rounds`: a bounded seed-centred map" in readme
